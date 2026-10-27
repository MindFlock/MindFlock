"""Claude Code's question prompt (the AskUserQuestion tool), pinned against
golden screens.

``tests/unit/data/dialogs/claude2_ask_<state>_w100.screen.txt`` are REAL
captures (Claude Code 2.1.311, 2026-10-27, a 100x40 tmux pane) of one run
through the tool: two questions in one call (a single-select with
descriptions, then a multi-select), the checkboxes ticked by digit, the
review tab, the tool's result; then one question answered in the free-text
row. Each file's first line is a ``#!`` header naming the state and the
prompt that produced it (stripped by :func:`_screen`).

``claude2_ask_<state>_w80`` / ``_w60`` are those captures re-wrapped by
:func:`_rewrap` — checked to reproduce the w100 capture line for line at 100
columns — so the id is pinned across widths the way the ``claude2_*_w*``
goldens pin the permission dialogs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.providers import dialogs
from backend.providers.claude import ClaudeProvider

DATA = Path(__file__).parent / "data" / "dialogs"

LIVE = (
    "tabs",
    "multi",
    "multi_one",
    "multi_two",
    "review",
    "single",
    "text",
    "typed",
)
ANSWERED = ("answered", "typed_answered")
NARROW = (80, 60)


def _screen(name: str) -> str:
    text = (DATA / ("%s.screen.txt" % name)).read_text(encoding="utf-8")
    first, _, rest = text.partition("\n")
    assert first.startswith("#! "), "%s has no #! provenance header" % name
    return rest


def _ask(state: str, width: int = 100) -> str:
    return _screen("claude2_ask_%s_w%d" % (state, width))


def _parse(screen: str):
    return ClaudeProvider().parse_dialog(screen)


def _id(screen: str) -> str:
    return dialogs.describe(_parse(screen), screen)["id"]


# --------------------------------------------------------------------------- #
# Re-wrapping a capture to another width                                       #
# --------------------------------------------------------------------------- #
def _wrap(text: str, cols: int) -> list:
    """``text`` word-wrapped to ``cols`` (a word longer than a line is cut)."""
    rows = [""]
    for word in text.split(" "):
        while len(word) > cols:
            if rows[-1]:
                rows.append("")
            rows[-1], word = word[:cols], word[cols:]
            rows.append("")
        if rows[-1] and len(rows[-1]) + 1 + len(word) > cols:
            rows.append("")
        rows[-1] = (rows[-1] + " " + word) if rows[-1] else word
    return [r for r in rows if r] or [""]


def _hanging(first: str, rest: list, lead: int, cols: int) -> list:
    """A paragraph whose first ``lead`` columns are a prefix and whose
    continuation lines hang under its text, re-wrapped to ``cols``."""
    text = " ".join([first[lead:].strip()] + [r.strip() for r in rest])
    rows = _wrap(text, cols - lead)
    return [first[:lead] + rows[0]] + [" " * lead + r for r in rows[1:]]


_OPTION_ROW = re.compile(r"^(\s*(?:❯\s)?\s*[1-9]\.\s(?:\[[ ✔]\]\s)?)(.*)$")


def _rewrap(screen: str, width: int) -> str:
    """A 100-column capture drawn at ``width`` columns, the way Claude Code
    lays it out: rules span the pane; the transcript's paragraphs, the
    question, the review's lines and the footer wrap at the pane's edge (the
    user's prompt one column short of it); an
    option's label and description wrap under themselves. (Every tab row
    here fits at 40 columns and up, so no tab label is cut.)"""
    lines = screen.split("\n")
    rule = (
        next(
            i
            for i in range(len(lines) - 1, -1, -1)
            if dialogs._SOLID_RULE.match(lines[i])
            and i + 1 < len(lines)
            and dialogs._ASK_TAB_ROW.match(lines[i + 1])
        )
        if any(dialogs._ASK_TAB_ROW.match(ln) for ln in lines)
        else len(lines)
    )
    out: list = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if dialogs._SOLID_RULE.match(line) and line.strip():
            out.append("─" * width)
            i += 1
            continue
        if i < rule:
            # Transcript: "❯ " / "● " / "  ⎿  " paragraphs hang under their text.
            lead = 5 if line.startswith("  ⎿  ") else 2
            para = line[:1] in ("❯", "●") or line.startswith("  ⎿  ")
            para = para or (line[:2] == "  " and line[2:3] not in ("", " "))
            if para:
                j = i + 1
                while (
                    j < len(lines)
                    and len(lines[j]) > lead
                    and lines[j][:lead] == " " * lead
                    and not lines[j][lead].isspace()
                    and not lines[j].lstrip().startswith("⎿")
                ):
                    j += 1
                # (the user's prompt keeps the last column free)
                cols = width - 1 if line[:1] == "❯" else width
                out += _hanging(line, lines[i + 1 : j], lead, cols)
                i = j
                continue
            out.append(line[:width])
            i += 1
            continue
        m = _OPTION_ROW.match(line)
        if m:
            out += _hanging(line, [], len(m.group(1)), width)
        elif line.strip().startswith(("●", "→")):
            lead = len(line) - len(line.lstrip()) + 2
            out += _hanging(line, [], lead, width)
        elif line.strip():
            indent = len(line) - len(line.lstrip())
            out += [" " * indent + r for r in _wrap(line.strip(), width - indent)]
        else:
            out.append(line)
        i += 1
    return "\n".join(out)


def _narrow_file(state: str, width: int) -> str:
    """The checked-in re-wrap's content (header included)."""
    return (
        "#! synthetic: claude2_ask_%s_w100 re-wrapped to %d columns by tests/unit/test_dialog_ask.py::_rewrap\n%s"
        % (
            state,
            width,
            _rewrap(_ask(state), width),
        )
    )


@pytest.mark.parametrize("state", LIVE + ANSWERED)
def test_the_rewrap_draws_the_capture_itself_at_its_own_width(state):
    assert _rewrap(_ask(state), 100) == _ask(state)


@pytest.mark.parametrize("state", LIVE)
@pytest.mark.parametrize("width", NARROW)
def test_the_narrow_goldens_are_the_rewrap(state, width):
    path = DATA / ("claude2_ask_%s_w%d.screen.txt" % (state, width))
    assert path.read_text(encoding="utf-8") == _narrow_file(state, width)
    assert max(len(ln) for ln in _ask(state, width).split("\n")) <= width


# --------------------------------------------------------------------------- #
# The parse                                                                    #
# --------------------------------------------------------------------------- #
NAV = {"next": ["Right"], "prev": ["Left"], "submit": ["Enter"]}
TWO_TABS = [
    {"label": "Color", "done": False},
    {"label": "Toppings", "done": False},
    {"label": "Submit", "done": False, "submit": True},
]


def _tabs(color: bool, toppings: bool) -> list:
    return [
        {"label": "Color", "done": color},
        {"label": "Toppings", "done": toppings},
        {"label": "Submit", "done": False, "submit": True},
    ]


def _o(key, label, kind, desc=None, checked=None, keys=None):
    return {
        "key": key,
        "label": label,
        "kind": kind,
        "keys": keys or [key],
        "desc": desc,
        "checked": checked,
    }


def _colors():
    return [
        _o("1", "Red", "other", "warm"),
        _o("2", "Green", "other", "calm"),
        _o("3", "Blue", "other", "default"),
        _o("4", "Type something.", "text"),
        _o("5", "Chat about this", "chat"),
    ]


def _toppings(*ticked):
    return [
        _o("1", "Cheese", "other", checked="Cheese" in ticked),
        _o("2", "Olives", "other", checked="Olives" in ticked),
        _o("3", "Mushrooms", "other", checked="Mushrooms" in ticked),
        _o("4", "Type something", "text", checked=False),
        _o("5", "Chat about this", "chat"),
    ]


def _frameworks(typing=False, typed=None):
    up = (lambda k: ["Up", k]) if typing else (lambda k: [k])
    return [
        _o("1", "React", "other", "what the app uses", keys=up("1")),
        _o("2", "Svelte", "other", "smaller", keys=up("2")),
        _o("3", "Plain HTML", "other", "no build", keys=up("3")),
        _o("4", typed or "Type something.", "text"),
        _o("5", "Chat about this", "chat", keys=up("5")),
    ]


def _want(
    heading,
    question,
    options,
    steps,
    step,
    *,
    multi=False,
    cursor="1",
    button=None,
    review=None,
):
    return {
        "heading": heading,
        "question": question,
        "command": None,
        "detail": None,
        "options": options,
        "source": None,
        "kind": "select",
        "multi": multi,
        "steps": steps,
        "step": step,
        "review": review,
        "cursor": cursor,
        "button": button,
        "nav": NAV,
    }


_COLOR_Q = "Which color should the button be?"
_TOPPINGS_Q = "Which toppings do you want?"
_FRAMEWORK_Q = "Which framework should the new page use?"
_FRAMEWORK_TAB = [{"label": "Framework", "done": False}]

GOLDEN = {
    "tabs": _want("Color", _COLOR_Q, _colors(), TWO_TABS, 0),
    "multi": _want(
        "Toppings",
        _TOPPINGS_Q,
        _toppings(),
        _tabs(True, False),
        1,
        multi=True,
        button="Submit",
    ),
    "multi_one": _want(
        "Toppings",
        _TOPPINGS_Q,
        _toppings("Cheese"),
        _tabs(True, True),
        1,
        multi=True,
        button="Submit",
    ),
    "multi_two": _want(
        "Toppings",
        _TOPPINGS_Q,
        _toppings("Cheese", "Mushrooms"),
        _tabs(True, True),
        1,
        multi=True,
        button="Submit",
    ),
    "review": _want(
        "Submit",
        "Ready to submit your answers?",
        [_o("1", "Submit answers", "yes"), _o("2", "Cancel", "no")],
        _tabs(True, True),
        2,
        review=[
            {"question": _COLOR_Q, "answer": "Red"},
            {"question": _TOPPINGS_Q, "answer": "Cheese, Mushrooms"},
        ],
    ),
    "single": _want("Framework", _FRAMEWORK_Q, _frameworks(), _FRAMEWORK_TAB, 0),
    "text": _want(
        "Framework",
        _FRAMEWORK_Q,
        _frameworks(typing=True),
        _FRAMEWORK_TAB,
        0,
        cursor="4",
    ),
    "typed": _want(
        "Framework",
        _FRAMEWORK_Q,
        _frameworks(typing=True, typed="Vue please"),
        _FRAMEWORK_TAB,
        0,
        cursor="4",
    ),
}


@pytest.mark.parametrize("width", (100,) + NARROW)
@pytest.mark.parametrize("state", LIVE)
def test_golden_screen_parses(state, width):
    got = _parse(_ask(state, width))
    assert got is not None
    region = got.pop("region")
    assert got == GOLDEN[state]
    # Cursor-, checkbox- and whitespace-free.
    assert "❯" not in region and "[" not in region and " " not in region


@pytest.mark.parametrize("width", (100, 40))
@pytest.mark.parametrize("state", LIVE)
def test_a_narrow_pane_still_parses(state, width):
    """At 40 columns the footer (and the framework question) wrap."""
    screen = _rewrap(_ask(state), width)
    got = _parse(screen)
    assert got is not None
    got.pop("region")
    assert got == GOLDEN[state]


@pytest.mark.parametrize("state", ANSWERED)
def test_the_tools_result_is_no_dialog(state):
    """Answered, the tool's result ("● User answered Claude's questions:")
    sits in the transcript over the input box: no parse, nothing on screen."""
    screen = _ask(state)
    assert _parse(screen) is None
    body = dialogs.describe(None, screen)
    assert body["parsed"] is False and body["options"] == []
    assert ClaudeProvider().dialog_on_screen(screen) is False


@pytest.mark.parametrize("state", LIVE)
def test_the_fixtures_use_the_providers_own_wording(state):
    """Every live state matches ClaudeProvider's waiting patterns — the
    classifier reads it as ``clarify`` even without the parse."""
    screen = _ask(state)
    pats = ClaudeProvider().waiting_prompt_patterns()
    assert any(re.search(p, screen) for p in pats), state
    assert ClaudeProvider().dialog_on_screen(screen) is True


# --------------------------------------------------------------------------- #
# describe(): the /dialog body                                                 #
# --------------------------------------------------------------------------- #
def test_describe_a_single_select_question():
    screen = _ask("tabs")
    body = dialogs.describe(_parse(screen), screen)
    assert body == {
        "id": body["id"],
        "parsed": True,
        "kind": "select",
        "question": _COLOR_Q,
        "heading": "Color",
        "command": None,
        "options": _colors(),
        "multi": False,
        "steps": TWO_TABS,
        "step": 0,
        "cursor": "1",
        "button": None,
        "nav": NAV,
    }
    assert re.fullmatch(r"[0-9a-f]{12}", body["id"])


def test_describe_a_multi_select_question():
    screen = _ask("multi_two")
    body = dialogs.describe(_parse(screen), screen)
    assert body["kind"] == "select" and body["multi"] is True
    assert [o["checked"] for o in body["options"]] == [True, False, True, False, None]
    assert body["button"] == "Submit" and body["step"] == 1
    assert "review" not in body


def test_describe_the_review_screen():
    screen = _ask("review")
    body = dialogs.describe(_parse(screen), screen)
    assert body["kind"] == "select"
    assert body["question"] == "Ready to submit your answers?"
    assert body["heading"] == "Submit" and body["step"] == 2
    assert body["review"] == GOLDEN["review"]["review"]
    assert [(o["label"], o["kind"]) for o in body["options"]] == [
        ("Submit answers", "yes"),
        ("Cancel", "no"),
    ]


def test_describe_leaves_other_dialogs_alone():
    """A permission prompt's body gains none of the select fields."""
    screen = (DATA / "claude2_stop_session_w100.screen.txt").read_text(encoding="utf-8")
    body = dialogs.describe(_parse(screen), screen)
    assert set(body) == {"id", "parsed", "question", "command", "options"}
    assert all(set(o) <= {"key", "label", "kind", "keys"} for o in body["options"])


# --------------------------------------------------------------------------- #
# The id                                                                       #
# --------------------------------------------------------------------------- #
def test_ticking_boxes_keeps_the_id():
    assert _id(_ask("multi")) == _id(_ask("multi_one")) == _id(_ask("multi_two"))


def test_moving_the_cursor_and_typing_keep_the_id():
    """``4`` moves the cursor onto the free-text row, typing fills it: the
    same question all along, so a client can post the digit and then the
    text with one id."""
    assert _id(_ask("single")) == _id(_ask("text")) == _id(_ask("typed"))


def test_moving_the_cursor_keeps_the_id():
    screen = _ask("tabs")
    moved = screen.replace("❯ 1. Red", "  1. Red").replace("  3. Blue", "❯ 3. Blue")
    assert moved != screen
    assert _parse(moved)["cursor"] == "3"
    assert _id(moved) == _id(screen)


@pytest.mark.parametrize("state", LIVE)
@pytest.mark.parametrize("width", NARROW + (40,))
def test_the_id_does_not_depend_on_the_width(state, width):
    assert _id(_rewrap(_ask(state), width)) == _id(_ask(state))
    if width in NARROW:
        assert _id(_ask(state, width)) == _id(_ask(state))


def test_each_question_and_the_review_have_their_own_id():
    ids = [_id(_ask(s)) for s in ("tabs", "multi", "review", "single")]
    assert len(set(ids)) == 4


def test_answering_the_question_changes_the_tab_row_not_the_id():
    """The tab row's ☐/☒ change as questions are answered (and its labels
    are cut with the width): left out of the id."""
    screen = _ask("tabs")
    ticked = screen.replace("☐ Color", "☒ Color")
    assert _parse(ticked)["steps"][0]["done"] is True
    assert _id(ticked) == _id(screen)


# --------------------------------------------------------------------------- #
# Which tab is up                                                              #
# --------------------------------------------------------------------------- #
def test_the_active_tab_comes_from_the_colours():
    """Claude draws the active tab on a background colour alone; a coloured
    capture (what /dialog reads) says which tab that is."""
    screen = _ask("tabs")
    lit = screen.replace(
        " ☐ Toppings ",
        "\x1b[48;2;177;185;249m\x1b[38;2;0;0;0m ☐ Toppings \x1b[39m\x1b[49m",
    )
    got = _parse(lit)
    assert got["step"] == 1 and got["heading"] == "Toppings"
    lit_first = screen.replace(" ☐ Color ", "\x1b[48;5;104m ☐ Color \x1b[49m")
    assert _parse(lit_first)["step"] == 0
    # The colours never change the id.
    assert _id(lit) == _id(screen)


def test_a_multi_select_next_button_is_not_the_last_question():
    screen = _ask("multi").replace("     Submit\n", "     Next\n")
    got = _parse(screen)
    assert got["button"] == "Next"
    # No colours, Toppings not answered: the label the question uses.
    assert got["step"] == 1


def test_the_narrowest_tab_row_names_only_the_active_tab():
    screen = _ask("tabs").replace(
        "←  ☐ Color  ☐ Toppings  ✔ Submit  →", "←  ☐   ☐ Top  ✔ Submit  →"
    )
    got = _parse(screen)
    assert got["step"] == 1 and got["heading"] == "Top"


def test_the_review_screen_skips_the_not_answered_warning():
    screen = _ask("review").replace(
        "Review your answers\n",
        "Review your answers\n\n⚠ You have not answered all questions\n",
    )
    got = _parse(screen)
    assert got["review"] == GOLDEN["review"]["review"]
    assert got["question"] == "Ready to submit your answers?"


# --------------------------------------------------------------------------- #
# Not this layout                                                              #
# --------------------------------------------------------------------------- #
def test_a_tab_row_needs_the_dialogs_top_rule():
    screen = _ask("tabs").replace("─" * 100 + "\n←", "\n←", 1)
    assert (
        dialogs._claude_ask(
            dialogs.normalize_lines(screen),
            screen.split("\n"),
            dialogs._drawn_widths(screen),
        )
        is None
    )


def test_transcript_under_the_choices_is_no_dialog():
    screen = _ask("tabs").rstrip("\n") + "\n● Something the agent printed later\n"
    assert _parse(screen) is None


def test_the_choices_need_the_cursor():
    screen = _ask("tabs").replace("❯ 1. Red", "  1. Red")
    assert _parse(screen) is None


def test_a_long_description_wraps_under_its_label():
    """A description wraps under itself, a label that runs to the pane's
    edge under the label: both read back whole, the id the same at any
    width."""
    long_desc = "what the app already uses for every other page in the repository"
    long_label = "Plain HTML with a sprinkle of hand written vanilla JavaScript"
    wide = (
        _ask("single")
        .replace("     what the app uses", "     " + long_desc)
        .replace("3. Plain HTML", "3. " + long_label)
    )
    for width in (100, 60, 40):
        screen = _rewrap(wide, width)
        got = _parse(screen)
        assert got is not None, width
        opts = {o["key"]: o for o in got["options"]}
        assert opts["1"]["desc"] == long_desc, width
        assert opts["3"]["label"] == long_label, width
        assert opts["3"]["desc"] == "no build", width
        assert _id(screen) == _id(wide)
