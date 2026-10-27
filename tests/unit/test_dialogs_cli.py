"""Dialog parsing for aider, opencode, Cline, goose and Antigravity, pinned
against golden screens.

``tests/unit/data/dialogs/<cli>_<what>_w<cols>.screen.txt`` — each file's
first line is a ``#!`` header naming where the screen came from (stripped
by :func:`_screen`):

* aider, opencode, Cline and goose screens are REAL captures (2026-10-26:
  aider 0.86.2, opencode 1.17.15, cline 3.0.70, goose 1.41.0) from a private
  tmux server. aider's confirm prompts need no model (``/run``, ``/add``, a
  message naming URLs); the tool permissions and questions came from the
  CLIs talking to a local mock OpenAI-compatible server that answers with
  one tool call. opencode, Cline and goose were captured with ``-e`` (the
  colour escapes ``/dialog`` reads): opencode marks its selected button by
  colour alone.
* Antigravity (agy) screens are RECONSTRUCTED from the live-captured
  snippets recorded in config.py and test_provider_activity_patterns.py —
  agy could not be run without writing into the owner's ~/.gemini store.

Each golden is parsed by its provider's ``parse_dialog`` and compared field
by field, ``keys`` included; the same dialog at another width (or with the
cursor moved) keeps its id; screens that are NOT a live dialog parse to
None.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from backend import providers
from backend.providers import dialogs
from backend.providers.config import (
    BUILTIN_CONFIGS,
    ProviderConfig,
    _config_from_toml,
)
from backend.providers.generic import GenericProvider
from backend.web.core import agent_io
from backend.web.core import proc

DATA = Path(__file__).parent / "data" / "dialogs"


def _screen(name: str) -> str:
    text = (DATA / ("%s.screen.txt" % name)).read_text(encoding="utf-8")
    first, _, rest = text.partition("\n")
    assert first.startswith("#! "), "%s has no #! provenance header" % name
    return rest


def _cli(name: str) -> str:
    return name.split("_", 1)[0]


def _prov(name: str):
    return providers.resolve(_cli(name))


def _parse(name: str):
    return _prov(name).parse_dialog(_screen(name))


def _o(key, label, kind, keys):
    return {"key": key, "label": label, "kind": kind, "keys": keys}


_AIDER_GROUP = [
    _o("Y", "Yes", "yes", ["y", "Enter"]),
    _o("N", "No", "no", ["n", "Enter"]),
    _o("A", "All", "always", ["a", "Enter"]),
    _o("S", "Skip all", "no", ["s", "Enter"]),
    # aider's "(D)on't ask again" REFUSES and remembers (confirm_ask
    # returns False): a "no", never a standing approval.
    _o("D", "Don't ask again", "no", ["d", "Enter"]),
]
_AIDER_YN = _AIDER_GROUP[:2]
_GOOSE_LABELS = [
    ("Allow", "yes"),
    ("Always Allow", "always"),
    ("Deny", "no"),
    ("Cancel", "no"),
]


def _goose(cursor):
    out = []
    for k, (label, kind) in enumerate(_GOOSE_LABELS):
        step = "Down" if k > cursor else "Up"
        out.append(_o(str(k + 1), label, kind, [step] * abs(k - cursor) + ["Enter"]))
    return out


GOLDEN = {
    "aider_url_w120": (
        {
            "heading": None,
            "question": "Add URL to the chat?",
            "command": None,
            "detail": "https://example.com/a",
            "options": _AIDER_GROUP,
        },
        "Add URL to the chat? https://example.com/a",
    ),
    "aider_create_w60": (
        {
            "heading": None,
            "question": "No files matched "
            "'docs/notes/release-checklist-for-the-next-version.md'. Do you want "
            "to create /tmp/ask-dialogs-scratch/docs/notes/"
            "release-checklist-for-the-next-version.md?",
            "command": None,
            "detail": None,
            "options": _AIDER_YN,
        },
        None,
    ),
    "aider_run_output_w120": (
        {
            "heading": None,
            "question": "Add 0.0k tokens of command output to the chat?",
            "command": None,
            "detail": None,
            "options": _AIDER_YN,
        },
        "Add 0.0k tokens of command output to the chat?",
    ),
    "aider_shell_w120": (
        {
            "heading": None,
            "question": "Run shell command?",
            "command": "git log --oneline -3 -- services/search/limits.py",
            "detail": None,
            "options": _AIDER_YN + [_AIDER_GROUP[4]],
        },
        "Run shell command?",
    ),
    "opencode_bash_w120": (
        {
            "heading": "Permission required",
            "question": "",
            "command": "git log --oneline -3 && ls -la",
            "detail": "Shell command",
            "options": [
                _o("1", "Allow once", "yes", ["Enter"]),
                _o("2", "Allow always", "always", ["Right", "Enter"]),
                _o("3", "Reject", "no", ["Escape"]),
            ],
        },
        "Permission required — Shell command",
    ),
    "opencode_bash_always_w80": (
        {
            "heading": "Permission required",
            "question": "",
            "command": "git log --oneline -3 && ls -la",
            "detail": "Shell command",
            # "Allow always" is the highlighted button (read from colour).
            "options": [
                _o("1", "Allow once", "yes", ["Left", "Enter"]),
                _o("2", "Allow always", "always", ["Enter"]),
                _o("3", "Reject", "no", ["Escape"]),
            ],
        },
        "Permission required — Shell command",
    ),
    "opencode_always_confirm_w120": (
        {
            "heading": "Always allow",
            "question": "",
            "command": "git log *\nls *",
            "detail": "This will allow the following patterns until OpenCode is "
            "restarted",
            "options": [
                _o("1", "Confirm", "always", ["Enter"]),
                _o("2", "Cancel", "other", ["Escape"]),
            ],
        },
        "Always allow — This will allow the following patterns until OpenCode "
        "is restarted",
    ),
    "opencode_write_w120": (
        {
            "heading": "Permission required",
            "question": "",
            "command": None,
            "detail": "Edit notes/todo.md",
            "options": [
                _o("1", "Allow once", "yes", ["Enter"]),
                _o("2", "Allow always", "always", ["Right", "Enter"]),
                _o("3", "Reject", "no", ["Escape"]),
            ],
        },
        "Permission required — Edit notes/todo.md",
    ),
    "opencode_question_w120": (
        {
            "heading": None,
            "question": "Which rate limiter should the search service use?",
            "command": None,
            "detail": None,
            "options": [
                _o("1", "Token bucket (Recommended)", "other", ["1"]),
                _o("2", "Sliding window", "other", ["2"]),
                _o("3", "Fixed window", "other", ["3"]),
                _o("4", "Type your own answer", "other", ["4"]),
            ],
        },
        "Which rate limiter should the search service use?",
    ),
    "goose_shell_w120": (
        {
            "heading": "shell",
            "question": "Goose would like to call the above tool, do you allow?",
            "command": "git log --oneline -3 && ls -la",
            "detail": None,
            "options": _goose(0),
        },
        "shell. Goose would like to call the above tool, do you allow?",
    ),
    "goose_shell_down_w80": (
        {
            "heading": "shell",
            "question": "Goose would like to call the above tool, do you allow?",
            "command": "git log --oneline -3 && ls -la",
            "detail": None,
            "options": _goose(1),
        },
        None,
    ),
    "goose_write_w80": (
        {
            "heading": "write",
            "question": "Goose would like to call the above tool, do you allow?",
            "command": None,
            "detail": "path /tmp/ask-dialogs-scratch/notes/todo.md",
            "options": _goose(0),
        },
        "write — path /tmp/ask-dialogs-scratch/notes/todo.md. Goose would like "
        "to call the above tool, do you allow?",
    ),
    "cline_command_w120": (
        {
            "heading": "Cline needs permission",
            "question": "Approve tool call?",
            "command": "git log --oneline -3 && ls -la",
            "detail": "run_commands",
            "options": [
                _o("y", "Approve", "yes", ["y"]),
                _o("n", "Deny", "no", ["n"]),
            ],
        },
        "Cline needs permission — run_commands. Approve tool call?",
    ),
    "cline_editor_w120": (
        {
            "heading": "Cline needs permission",
            "question": "Approve tool call?",
            # The editor's "command" argument is its operation, not a shell
            # command.
            "command": None,
            "detail": "editor notes/todo.md",
            "options": [
                _o("y", "Approve", "yes", ["y"]),
                _o("n", "Deny", "no", ["n"]),
            ],
        },
        "Cline needs permission — editor notes/todo.md. Approve tool call?",
    ),
    "cline_question_w80": (
        {
            "heading": None,
            "question": "Which rate limiter should the search service use?",
            "command": None,
            "detail": None,
            # The free-text row ("Type a response...") is no button.
            "options": [
                _o("1", "Token bucket", "other", ["1"]),
                _o("2", "Sliding window", "other", ["2"]),
                _o("3", "Fixed window", "other", ["3"]),
            ],
        },
        "Which rate limiter should the search service use?",
    ),
    "antigravity_permission_w120": (
        {
            "heading": "Requesting permission",
            "question": "Do you want to proceed?",
            "command": "touch build/.stamp && make -C services/search lint",
            "detail": None,
            "options": [
                _o("1", "Yes", "yes", ["Enter"]),
                _o(
                    "2",
                    "Yes, and always allow in this conversation",
                    "always",
                    ["Down", "Enter"],
                ),
                _o("3", "No", "no", ["Down", "Down", "Enter"]),
            ],
        },
        "Requesting permission. Do you want to proceed?",
    ),
    "antigravity_permission_w60": (
        {
            "heading": "Requesting permission",
            "question": "Do you want to proceed?",
            "command": "touch build/.stamp && make -C services/search\nlint",
            "detail": None,
            "options": [
                _o("1", "Yes", "yes", ["Up", "Enter"]),
                _o(
                    "2",
                    "Yes, and always allow in this conversation",
                    "always",
                    ["Enter"],
                ),
                _o("3", "No", "no", ["Down", "Enter"]),
            ],
        },
        None,
    ),
    "antigravity_question_w100": (
        {
            "heading": None,
            "question": "What kind of task are we working on today?",
            "command": None,
            "detail": None,
            "options": [
                _o(
                    "1",
                    "I want to start a new web application from scratch.",
                    "other",
                    ["Enter"],
                ),
                _o(
                    "2",
                    "I want to debug or modify an existing codebase.",
                    "other",
                    ["Down", "Enter"],
                ),
                _o(
                    "3",
                    "I want to understand how this codebase works.",
                    "other",
                    ["Down", "Down", "Enter"],
                ),
                _o("4", "Write-in...", "other", ["Down", "Down", "Down", "Enter"]),
            ],
        },
        "What kind of task are we working on today?",
    ),
}

#: The same dialog at several widths / cursor positions / redraws.
SAME_DIALOG = [
    ("aider_url_w120", "aider_url_w60", "aider_url_resized_w60"),
    ("aider_shell_w120", "aider_shell_w50"),
    (
        "opencode_bash_w120",
        "opencode_bash_always_w80",
        "opencode_bash_always_w70",
    ),
    ("opencode_write_w120", "opencode_write_w80"),
    ("opencode_question_w120", "opencode_question_w80"),
    ("goose_shell_w120", "goose_shell_w80", "goose_shell_down_w80"),
    ("cline_command_w120", "cline_command_w80"),
    ("cline_question_w80", "cline_question_down_w120"),
    ("antigravity_permission_w120", "antigravity_permission_w60"),
]

#: Screens that are NOT a live dialog: an answered prompt, a list the agent
#: printed, the user's own numbered input.
NOT_A_DIALOG = [
    "aider_answered_w60",
    "opencode_printed_list_w80",
    "opencode_idle_w80",
    "goose_denied_w80",
    "cline_denied_w80",
    "antigravity_input_numbered_w100",
    "antigravity_printed_list_w100",
]

ALL_LIVE = sorted({n for group in SAME_DIALOG for n in group} | set(GOLDEN))


# --------------------------------------------------------------------------- #
# The goldens                                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_golden_screen_parses(name):
    expected, _ = GOLDEN[name]
    got = _parse(name)
    assert got is not None, name
    for field, want in expected.items():
        assert got[field] == want, (name, field, got[field])
    assert got["source"] is None
    assert got["region"]


@pytest.mark.parametrize("name", sorted(n for n in GOLDEN if GOLDEN[n][1]))
def test_golden_screen_describe(name):
    expected, question = GOLDEN[name]
    body = dialogs.describe(_parse(name), _screen(name))
    assert body["parsed"] is True
    assert body["question"] == question
    assert body["command"] == expected["command"]
    # describe() carries each option's keys through: the UI posts them.
    assert body["options"] == expected["options"]
    assert "source" not in body


def test_golden_ids_are_all_different():
    ids = {}
    for name in GOLDEN:
        ids.setdefault(dialogs.describe(_parse(name), _screen(name))["id"], []).append(
            name
        )
    same = {frozenset(g) for g in SAME_DIALOG}
    for names in ids.values():
        if len(names) > 1:
            assert any(set(names) <= g for g in same), names


@pytest.mark.parametrize("group", SAME_DIALOG, ids=lambda g: g[0])
def test_one_dialog_has_one_id_at_every_width_and_cursor(group):
    """Resizing re-wraps the dialog, arrowing moves the selection (goose's
    hint moves with it), a resize leaves a stale copy of aider's prompt —
    the id stays, or the next click is refused as 'the prompt changed'."""
    ids = {dialogs.describe(_parse(n), _screen(n))["id"] for n in group}
    assert len(ids) == 1, group


def test_a_different_subject_is_a_different_dialog():
    one = dialogs.describe(_parse("aider_url_w60"), _screen("aider_url_w60"))
    other = dialogs.describe(
        _parse("aider_url_other_w60"), _screen("aider_url_other_w60")
    )
    assert one["id"] != other["id"]
    assert _parse("aider_url_other_w60")["detail"] == "https://example.org/b"


@pytest.mark.parametrize("name", NOT_A_DIALOG)
def test_not_a_live_dialog(name):
    assert _parse(name) is None
    body = dialogs.describe(None, _screen(name))
    assert body["parsed"] is False and body["options"] == []


@pytest.mark.parametrize(
    "name", ["opencode_idle_w80", "cline_denied_w80", "antigravity_input_numbered_w100"]
)
def test_no_dialog_on_screen(name):
    assert _prov(name).dialog_on_screen(_screen(name)) is False


@pytest.mark.parametrize("name", ALL_LIVE)
def test_every_live_dialog_is_on_screen(name):
    assert _prov(name).dialog_on_screen(_screen(name)) is True


@pytest.mark.parametrize("name", ALL_LIVE)
def test_the_fixtures_use_the_providers_own_wording(name):
    """Each golden matches its provider's waiting patterns, so the fixtures
    cannot drift from what the classifier flags as ``clarify``."""
    screen = dialogs.strip_ansi(_screen(name))
    pats = _prov(name).waiting_prompt_patterns()
    assert any(re.search(p, screen) for p in pats), name


@pytest.mark.parametrize("name", ALL_LIVE)
def test_every_key_is_an_answer_key(name):
    for opt in _parse(name)["options"]:
        assert opt["keys"], (name, opt)
        _, keys, err = agent_io.parse_answer({"keys": opt["keys"]})
        assert err is None and keys == opt["keys"], (name, opt)


# --------------------------------------------------------------------------- #
# Colour escapes                                                               #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", [n for n in ALL_LIVE if not n.startswith("opencode")])
def test_colour_escapes_change_nothing(name):
    """A ``-e`` capture parses exactly like the plain one."""
    screen = _screen(name)
    plain = dialogs.strip_ansi(screen)
    assert _prov(name).parse_dialog(plain) == _prov(name).parse_dialog(screen)


def test_without_colours_opencode_assumes_its_initial_selection():
    screen = _screen("opencode_bash_always_w80")
    coloured = _prov("opencode_x").parse_dialog(screen)
    plain = _prov("opencode_x").parse_dialog(dialogs.strip_ansi(screen))
    assert [o["keys"] for o in coloured["options"]][0] == ["Left", "Enter"]
    assert [o["keys"] for o in plain["options"]][0] == ["Enter"]
    assert dialogs.dialog_id(coloured) == dialogs.dialog_id(plain)


def _paint(screen: str) -> str:
    """``screen`` as ``capture-pane -e`` might draw it: every word wrapped in
    colours, a reset at each line end."""
    out = []
    for line in screen.split("\n"):
        line = re.sub(r"(\S+)", "\x1b[38;5;110;48;5;234m\\1\x1b[39m", line)
        out.append(line + ("\x1b[0m" if line else ""))
    return "\n".join(out)


@pytest.mark.parametrize(
    "name,prov",
    [
        ("claude_bash", "claude"),
        ("claude2_spawn_w79", "claude"),
        ("claude2_bash_side_panel", "claude"),
        ("codex_exec", "codex"),
        ("codex_trust", "codex"),
    ],
)
def test_claude_and_codex_read_a_coloured_capture_the_same(name, prov):
    """``/dialog`` now captures with ``-e`` for every provider."""
    text = (DATA / ("%s.screen.txt" % name)).read_text(encoding="utf-8")
    p = providers.resolve(prov)
    plain = dialogs.describe(p.parse_dialog(text), text)
    painted = dialogs.describe(p.parse_dialog(_paint(text)), _paint(text))
    assert painted == plain


def test_highlighted_reads_the_button_background():
    gap, lit = "\x1b[48;5;234m", "\x1b[48;5;215m"
    row = gap + "  " + lit + " Allow once " + gap + "  Allow always   Reject"
    labels = ("Allow once", "Allow always", "Reject")
    assert dialogs._highlighted(row, labels) == 0
    row = gap + "   Allow once  " + lit + " Allow always " + gap + "  Reject"
    assert dialogs._highlighted(row, labels) == 1
    assert dialogs._highlighted(dialogs.strip_ansi(row), labels) is None
    # Two buttons: told apart from the gap's background, not each other's.
    row = gap + "  " + lit + " Confirm " + gap + "  Cancel"
    assert dialogs._highlighted(row, ("Confirm", "Cancel")) == 0


def test_current_dialog_captures_the_colours(monkeypatch):
    calls = []
    screen = _screen("opencode_bash_always_w80")

    def run(args, **kw):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, screen, "")

    monkeypatch.setattr(proc, "run_capped", run)
    body, err = agent_io.current_dialog("mindflock_w", "opencode")
    assert err is None
    assert calls == [["tmux", "capture-pane", "-p", "-e", "-J", "-t", "mindflock_w"]]
    assert body["options"][1] == {
        "key": "2",
        "label": "Allow always",
        "kind": "always",
        "keys": ["Enter"],
    }


# --------------------------------------------------------------------------- #
# Free-text questions                                                          #
# --------------------------------------------------------------------------- #
def test_a_free_text_question_is_served_unparsed_with_its_question():
    name = "antigravity_writein_w100"
    parsed = _parse(name)
    assert parsed["options"] == []
    body = dialogs.describe(parsed, _screen(name))
    assert body["parsed"] is False and body["options"] == []
    assert body["question"] == "What should the new limiter key be called?"


def test_aider_with_a_half_typed_answer_is_no_parse():
    screen = _screen("aider_shell_w120").rstrip() + " y\n"
    assert dialogs.parse_aider(screen) is None


def test_goose_needs_exactly_one_selection():
    screen = dialogs.strip_ansi(_screen("goose_shell_w120"))
    assert dialogs.parse_goose(screen) is not None
    screen = screen.replace("○ Always Allow", "● Always Allow")
    assert dialogs.parse_goose(screen) is None


def test_opencode_needs_its_select_hint():
    screen = re.sub(
        r"⇆\s*select", "", dialogs.strip_ansi(_screen("opencode_bash_w120"))
    )
    assert dialogs.parse_opencode(screen) is None


def test_cline_answered_prompt_with_the_input_box_under_it_is_no_parse():
    screen = dialogs.strip_ansi(_screen("cline_command_w80"))
    screen += "─" * 60 + "\n❯ Ask anything...\n" + "─" * 60 + "\n"
    assert dialogs.parse_cline(screen) is None


# --------------------------------------------------------------------------- #
# Wiring: dialog_layout                                                        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cli", ["aider", "opencode", "cline", "goose", "antigravity"])
def test_the_bundled_cli_reads_its_dialogs(cli):
    cfg = next(c for c in BUILTIN_CONFIGS if c.name == cli)
    assert cfg.dialog_layout == cli
    assert providers.resolve(cli).parses_dialogs() is True


def test_a_plain_config_provider_keeps_todays_behaviour():
    p = GenericProvider(ProviderConfig(name="mycli", program_aliases=("mycli",)))
    assert p.parses_dialogs() is False
    assert p.parse_dialog(_screen("goose_shell_w120")) is None
    # Codex is a GenericProvider that reads its own dialogs.
    assert providers.resolve("codex").parses_dialogs() is True


@pytest.mark.parametrize("name", ALL_LIVE)
def test_each_layout_reads_only_its_own_cli(name):
    screen = _screen(name)
    for layout, parse in dialogs.LAYOUTS.items():
        if layout != _cli(name):
            assert parse(screen) is None, (layout, name)


def test_toml_dialog_layout():
    raw = {"provider": {"name": "mygoose"}, "classify": {"dialog_layout": "Goose"}}
    cfg = _config_from_toml(raw)
    assert cfg.dialog_layout == "goose"
    assert GenericProvider(cfg).parses_dialogs() is True
    assert GenericProvider(cfg).parse_dialog(_screen("goose_shell_w120"))["options"]
    assert _config_from_toml({"provider": {"name": "x"}}).dialog_layout == ""
    with pytest.raises(ValueError):
        _config_from_toml(
            {"provider": {"name": "x"}, "classify": {"dialog_layout": "vim"}}
        )


@pytest.mark.parametrize(
    "label,kind",
    [
        ("Always allow", "always"),
        ("Always Allow", "always"),
        ("Allow once", "yes"),
        ("Allow always", "always"),
        ("Reject", "no"),
        ("Deny", "no"),
    ],
)
def test_option_kind_for_the_new_clis(label, kind):
    assert dialogs.option_kind(label) == kind
