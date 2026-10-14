"""Calm surface, the pane header: source-level pins (the behaviour itself is
unit-tested in frontend/src/__tests__/{stage,autopilot,usageModel}.test.ts;
the fit at a 2×2 grid was checked with the screenshot harness).

Pins: session actions ask and fail on inline error cards, never
confirm()/alert() (a silent no-op in the desktop app, so Merge, Push anyway
and Clean up did nothing there); the guided next step's quiet variant; the
pane's hide control is − (✕ means end/close everywhere); the header's
copy-whole-history button and the history view's Copy all share one loader
(lib/paneHistory); the Assistant window's Instructions button.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


def _code(src: str) -> str:
    """The source with its comments removed, so a comment that NAMES confirm()
    (to say why it is not used) can't hide or fake a call."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(^|[^:\"'])//[^\n]*", r"\1", src)


def test_session_actions_never_confirm_or_alert():
    src = _read("lib/sessionActions.ts")
    code = _code(src)
    assert not re.search(r"\bconfirm\(", code)
    assert not re.search(r"\balert\(", code)
    # The old call sites, by name.
    for old in (
        'alert("Close failed: "',
        'alert("Copy failed: "',
        'alert(ideName() + ": "',
        "Merge this branch's PR into staging?",
        'Push anyway?")',
        "Clean up '",
    ):
        assert old not in src, old
    # No base branch is named for the user: the merge goes where the PR says.
    assert "into staging" not in src
    assert "staging" not in code


def test_session_actions_ask_on_inline_cards():
    src = _read("lib/sessionActions.ts")
    assert (
        "export async function cleanupMissing(title: string, confirmed = false)" in src
    )
    assert (
        '{ label: "Delete", primary: true, run: () => void cleanupMissing(title, true) }'
        in src
    )
    assert (
        "export async function mergeSession(title: string, overrideRedZones = false, confirmed = false)"
        in src
    )
    assert "if (!overrideRedZones && !confirmed) {" in src
    assert '"Merge this PR?"' in src
    assert (
        '{ label: "Merge PR", primary: true, run: () => void mergeSession(title, false, true) }'
        in src
    )
    # The card names its session: a nameless card deduped a second session's
    # block into the first card, whose Push anyway then pushed the wrong one.
    assert '"Push blocked — checks haven\'t passed",' in src
    assert (
        "displayName(title) + \"'s commit has no passing check run (see the ✗ checks chip on its row)."
        in src
    )
    assert 'errorPop("Checks haven\'t passed for this commit"' not in src
    assert 'label: "Push anyway"' in src
    # The red-zone override already given carries into the re-push
    # (test_frontend_attention pins "pushSession(title, true)").
    assert (
        "void (overrideRedZones ? pushSession(title, true, true) : pushSession(title, true))"
        in src
    )
    for what in (
        'errorPop("Close failed"',
        'errorPop("Copy failed"',
        'errorPop(ideName() + " failed"',
    ):
        assert what in src, what


def test_guided_next_step_has_a_quiet_variant():
    pane = _read("components/grid/Pane.tsx")
    # Appended AFTER the existing expression, whose prefix other pins read.
    assert re.search(
        r'"nextstep" \+\s*\(shownStep\.hint \? " nextstep-hint" : ""\) \+\s*'
        r'\(shownStep\.disabled \? " nextstep-blocked" : ""\) \+\s*'
        r'\(quiet \? " nextstep-quiet" : ""\)\s*\}',
        pane,
    )
    # Busy agent; a Commit… with nothing uncommitted isn't drawn at all.
    assert (
        'act === "working"' in pane
        and 'act === "clarify"' in pane
        and 'act === "limit"' in pane
    )
    assert 'ns && ns.label === "Commit…" && !hasUncommitted(inst) ? null : ns' in pane
    css = _read("components/grid/Pane.css")
    assert re.search(
        r"\.pane-head \.actions \.nextstep\.nextstep-quiet \{\s*background: transparent;\s*"
        r"border: 1px solid var\(--border\);\s*color: var\(--text\);",
        css,
    )


def test_fast_track_off_is_just_the_glyph():
    pane = _read("components/grid/Pane.tsx")
    assert '{ft.lane === "leave" && !ft.halted ? "⏩" : ft.label}' in pane
    assert '(ft.lane === "leave" ? "off" : ft.label.replace(/^⏩ /, ""))' in pane


def test_pane_hide_is_a_minus_not_a_cross():
    pane = _read("components/grid/Pane.tsx")
    # The text each pane-close button renders: what sits right before its
    # </button> (its props hold "=>", so no [^>]* match).
    closes = []
    for m in re.finditer(r'className="act pane-close"', pane):
        body = pane[m.end() : pane.index("</button>", m.end())]
        closes.append(body.rsplit(">", 1)[1].strip())
    assert len(closes) == 3, closes
    assert all(c == "−" for c in closes), closes
    assert "✕" not in _code(pane)
    assert 'aria-label="Hide window"' in pane
    assert (
        'const HIDE_TITLE = "Hide window — the session keeps running (show it again from its row)";'
        in pane
    )
    assert pane.count("title={HIDE_TITLE}") == 3
    # A special window's ✕ still closes it.
    special = _read("components/grid/SpecialPane.tsx")
    assert re.search(r'aria-label="Close window"[^<]*>\s*✕\s*</button>', special, re.S)


def test_pane_title_is_the_rail_label():
    pane = _read("components/grid/Pane.tsx")
    assert 'import { sessionLabel } from "../../lib/sessionLabel";' in pane
    assert "const label = sessionLabel(" in pane
    assert "const displayName = alias || label.text;" in pane
    assert pane.count('<span className="title" title={nameTip}>') == 3
    # A narrow head drops the "(tix) " kind tag, never the name.
    assert '<span className="title-kind">{kindTag}</span>' in pane
    css = _read("components/grid/Pane.css")
    assert re.search(
        r"@container \(max-width: 440px\) \{\s*\.pane-head \.title \.title-kind \{\s*display: none;",
        css,
    )


def test_copy_all_and_the_header_copy_share_one_loader():
    """The owner asked for "copy entire channel" back in the header (it had
    moved into the History view's bar): both now go through lib/paneHistory,
    so neither fetches /history itself nor copies differently."""
    pane = _read("components/grid/Pane.tsx")
    assert "/history?pane=" not in pane
    assert "copyText" not in pane
    assert 'aria-label="Copy this pane\'s whole history"' in pane
    assert "void copyPaneHistory(title, termTab);" in pane
    hist = _read("components/grid/HistoryOverlay.tsx")
    assert "Copy all" in hist
    assert 'className="hist-copy"' in hist
    assert "onClick={copyAll}" in hist
    assert "const copyAll = () => void copyPaneHistory(title, pane, text);" in hist
    assert "/history?pane=" not in hist
    assert "t = await fetchHistory(title, pane);" in hist
    lib = _read("lib/paneHistory.ts")
    # One fetch helper (through apiText, so a 401 reloads).
    assert lib.count("/history?pane=${pane}") == 1
    assert "return apiText(" in lib
    # Claude Code layout cleaned (agentCopy) on the way to the clipboard.
    assert "copyText(historyForClipboard(all, title, pane))" in lib
    assert "agentCopyText(" in lib


def test_narrow_heads_keep_the_cost():
    css = _read("components/grid/Pane.css")
    head = css[css.index(".pane-head {\n  container-type: inline-size;") :]
    narrow = head[head.index("@container (max-width: 1000px)") :]
    narrow = narrow[: narrow.index("\n}\n")]
    assert ".uh-prov" in narrow and ".uh-ctx" in narrow and ".uh-cost" not in narrow
    chip = _read("components/usage/SessionUsageChip.tsx")
    assert '<span className="usage-head">' in chip
    for part in ("uh-prov", "uh-cost", "uh-ctx"):
        assert f'className="{part}"' in chip, part


def test_assistant_agent_file_has_one_name_and_two_doors():
    """The owner looks for the "assistant agent file": both doors (the bar's
    button and the chat window's head) read "Instructions" — "Agent" is the
    coding agent's word alone (GLOSSARY) — and still say "agent file" in their
    tooltips and the palette row, so searching for it finds it."""
    special = _read("components/grid/SpecialPane.tsx")
    assert 'id="assistant-agent-head-btn"' in special
    assert 'openDialogFor("assistant-agent")' in special
    assert re.search(r">\s*Instructions\s*</button>", special)
    assert "the assistant's agent file" in special
    bars = _read("components/sidebar/SidebarBars.tsx")
    assert 'id="assistant-agent-btn"' in bars
    # One id per button: the head no longer reuses the bar's.
    assert 'id="assistant-agent-btn"' not in special
    dialog = _read("components/dialogs/AssistantAgentDialog.tsx")
    assert "<h2>Assistant instructions</h2>" in dialog
    assert 'id="assistant-agent-text"' in dialog  # editable in place
    assert 'label: "Assistant instructions (agent file)…"' in _read(
        "components/palette/CommandPalette.tsx"
    )


def test_stage_chip_says_question():
    # The word is lib/sessionState's (one label per state, the same everywhere);
    # the class stays the activity word "clarify".
    state = _read("lib/sessionState.ts")
    assert 'question: "Question",' in state
    assert 'label: "clarify"' not in state and 'label: "clarify"' not in _read(
        "lib/stage.ts"
    )
    assert 'needs_you: "s-clarify",' in _read("lib/stage.ts")


def test_thread_empty_state_points_at_split():
    tab = _read("components/grid/ThreadTab.tsx")
    assert "use Split into parallel pieces… in the session's › menu" in tab
    assert "fork button" not in tab
