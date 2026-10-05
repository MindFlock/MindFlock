"""Reading the dialog an agent CLI is blocked on, from its visible screen.

When a session's activity is ``clarify`` its CLI is showing a permission
prompt, a folder-trust gate or a question, and waits for a key. The web UI
answers it in place — a row of the dialog's OWN option buttons on the rail,
in the bell and in the Thread tab — so it needs the dialog as data: the
question, the command it asks about, and the numbered options.

Parsing is provider-owned (:meth:`BaseProvider.parse_dialog
<backend.providers.base.BaseProvider.parse_dialog>`): each CLI draws its
dialogs differently. This module holds the shared, pure pieces — screen
normalization, the numbered option block, option kinds, the dialog id — and
the two layouts known so far, Claude Code's (:func:`parse_claude`) and
Codex's (:func:`parse_codex`). Both are pinned against golden screens under
``tests/unit/data/dialogs/``.

A parse is deliberately conservative. It needs a numbered option list with
the CLI's selection cursor on one of its lines, at the bottom of the screen
(only blank lines, rules and a key-hint footer below it): an agent PRINTING
a numbered list never carries the cursor glyph, and a numbered prompt the
user typed sits in the transcript, above the input box. Anything else
returns None, and the UI falls back to the question line plus a button
that opens the pane — a wrong button is worse than no button.

A parsed dialog (``parse_*``'s return)::

    {"heading": str|None,   # the dialog's title line ("Bash command")
     "question": str,       # the question it asks ("Do you want to proceed?")
     "command": str|None,   # what it asks about, when that is a command
     "detail": str|None,    # one line of context (a description, a path)
     "options": [{"key": "1", "label": "Yes", "kind": "yes"}, ...],
     "source": str|None,    # who asked, from a tab header ("general-purpose
                            # agent" for a background sub-agent's prompt)
     "region": str}         # what the id hashes: one line per component
                            # (a paragraph, each option), cursor-free, with
                            # ALL whitespace removed and anything the width
                            # can cut reduced to a short prefix

The id must not depend on the terminal's width: resizing a pane (opening the
page, a new worker pane changing the layout) re-wraps a dialog's long lines,
and an id that moved with the wrapping refused the next click as "the prompt
changed" for the very prompt it was meant for. Wrapping only inserts line
breaks and indentation, so each component drops every whitespace character.
What the CLI cuts short with "…" depends on the width too — an option
naming a long path, a collapsed tool description — so such a component
keeps only what every width shows: its text before the cut, capped at
:data:`ID_PREFIX_CHARS` (see :func:`_id_key`). Nothing is ever left out
whole: the question, the command and every option's key are always in.

Option ``kind``: ``yes`` (approve this once), ``always`` (approve and stop
asking — a standing rule, a session-wide or mode switch), ``no`` (refuse /
exit / cancel) or ``other`` (anything else, e.g. a free-text choice).
"""

from __future__ import annotations

import hashlib
import re
from typing import List, Optional, Sequence, Tuple

__all__ = [
    "MAX_QUESTION_CHARS",
    "normalize_lines",
    "option_block",
    "option_kind",
    "clean_label",
    "compose_question",
    "dialog_id",
    "fallback_question",
    "describe",
    "dialog_on_screen",
    "parse_claude",
    "parse_codex",
]

#: Ceilings on the text fields a parse returns (the UI shows one line or two).
MAX_QUESTION_CHARS = 300
MAX_COMMAND_CHARS = 500
MAX_DETAIL_CHARS = 200
MAX_LABEL_CHARS = 120
#: How far above the option block a dialog's question may sit.
_REGION_LINES = 40

# A box border on either side of a line (Claude Code 1.x drew its dialogs in
# a rounded box; 2.x uses a top rule only).
_BORDER_LEFT = re.compile(r"^\s*[│┃║]\s?")
_BORDER_RIGHT = re.compile(r"\s?[│┃║]\s*$")
# A line made only of rule characters (a dialog's top edge or a separator),
# optionally with box corners.
_SOLID_RULE = re.compile(r"^\s*[╭┌╔]?[─━═]{3,}[╮┐╗]?\s*$")
_ANY_RULE = re.compile(r"^\s*[╭┌╔╰└╚]?[─━═╌┄┈╍┅┉-]{3,}[╮┐╗╯┘╝]?\s*$")
# A rule with text drawn on it ("──── ↓ 2 new messages ────"): a long run of
# rule characters at either end of the line. Still a dialog's top edge.
_TEXT_RULE = re.compile(r"^\s*[╭┌╔]?[─━═]{8,}|[─━═]{8,}[╮┐╗]?\s*$")
# A key-hint footer under a dialog's options ("Esc to cancel · Tab to amend",
# "Press enter to confirm or esc to cancel", "ctrl-g to edit in Vim").
_FOOTER = re.compile(r"\b(?:esc|enter|tab|ctrl|shift|return)\b", re.IGNORECASE)
_FOOTER_MAX_CHARS = 160
# One numbered option: an optional selection cursor, the number, the label.
_OPTION = re.compile(
    r"^(?P<indent>\s*)(?P<cursor>[❯›▶►>]\s*)?(?P<num>[1-9])[.)]\s+(?P<label>\S.*?)\s*$"
)
# The key hint a CLI prints after an option: "(esc)", "(y)", "(shift+tab)".
_KEY_HINT = re.compile(
    r"\s*\((?:esc|enter|tab|shift\+tab|ctrl\+[a-z]|[a-z])\)\s*$", re.IGNORECASE
)
_WS = re.compile(r"\s+")
# A selection cursor (removed before hashing a dialog): any of the CLIs'
# cursor glyphs, or a plain ">" in front of an option number.
_CURSOR_GLYPHS = re.compile(r"^\s*>\s*(?=[1-9][.)]\s)|[❯›▶►]")
# A dashed rule: what Claude Code 2.x draws between a dialog's sections (the
# command box, a diff, the tool's description) — never its top edge.
_DASHED_RULE = re.compile(r"^\s*[╌┄┈╍┅┉]{3,}\s*$")
# A line the CLI cut short to fit the width ("…uncommitted…"), and the hint
# it prints under a collapsed block — both change with the terminal's width.
_TRUNCATED = re.compile(r"…\s*$")
# The collapsed-block hint, matched with its whitespace removed (it wraps
# onto two lines in a narrow pane): "(ctrl+o to expand description)".
_EXPAND_HINT_KEY = re.compile(r"\(ctrl\+otoexpand[^()]*\)?", re.IGNORECASE)
#: How much of a component the width may cut ("…") goes into a dialog's id:
#: its whitespace-free text, capped here. Every width a dialog is usable at
#: shows at least this much of an option before cutting it (at 47 columns
#: option 2 still shows ~33 non-blank characters).
ID_PREFIX_CHARS = 24
# A vertical divider shared by most bottom lines, well right of the left
# border: a side panel (Claude Code's diff panel) drawn next to the dialog.
_PANEL_MIN_COL = 20
_PANEL_SHARE = 0.6

#: Label fragments that turn an approval into a STANDING one.
_ALWAYS_HINTS = (
    "don't ask again",
    "do not ask again",
    "always",
    "allow all",
    "for this session",
    "during this session",
    "for this conversation",
    "in the future",
    "auto-accept",
    "auto mode",
    "bypass",
)
_YES_WORDS = ("yes", "allow", "approve", "accept", "proceed", "continue", "trust")
_NO_WORDS = ("no", "deny", "reject", "cancel", "exit", "quit", "decline")


# --------------------------------------------------------------------------- #
# Shared pieces
# --------------------------------------------------------------------------- #
def _squash(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def _cap(text: Optional[str], limit: int) -> Optional[str]:
    if text is None:
        return None
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _strip_side_panel(raws: List[str]) -> List[str]:
    """``raws`` cut at a side panel's divider, when one is drawn.

    Claude Code can show a panel (its diff view) to the right of the
    conversation: every line then carries a ``│`` at the same column with the
    panel's own text after it, which no right-border rule removes and which
    would land in the middle of a dialog's question and options. A column
    where most of the bottom lines have a vertical bar, far enough right not
    to be a left border, is that divider."""
    tail = [ln for ln in raws[-40:] if ln.strip()]
    if len(tail) < 4:
        return raws
    counts: dict = {}
    for ln in tail:
        for col, ch in enumerate(ln):
            if ch in "│┃║" and col >= _PANEL_MIN_COL:
                counts[col] = counts.get(col, 0) + 1
    if not counts:
        return raws
    col, hits = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))
    if hits < _PANEL_SHARE * len(tail):
        return raws
    return [ln[:col] if len(ln) > col and ln[col] in "│┃║" else ln for ln in raws]


def normalize_lines(screen: str) -> List[str]:
    """The screen as lines with box borders, a side panel and trailing blanks
    removed.

    Right-trimmed only: indentation is meaningful (an option label wrapped
    onto the next line is indented under it)."""
    raws = [raw.rstrip() for raw in (screen or "").replace("\r", "").split("\n")]
    out = []
    for raw in _strip_side_panel(raws):
        line = _BORDER_RIGHT.sub("", _BORDER_LEFT.sub("", raw.rstrip()))
        out.append(line.rstrip())
    while out and not out[-1].strip():
        out.pop()
    return out


def _drawn_widths(screen: str) -> List[int]:
    """Each screen line's drawn width — borders included, a side panel cut
    away — index for index with :func:`normalize_lines`. Its maximum is the
    pane's width as far as the dialog is concerned: where a box wraps."""
    raws = [raw.rstrip() for raw in (screen or "").replace("\r", "").split("\n")]
    return [len(raw.rstrip()) for raw in _strip_side_panel(raws)]


def option_block(
    lines: Sequence[str], cursors: str, widths: Optional[Sequence[int]] = None
) -> Optional[Tuple[int, int, List[dict]]]:
    """The LAST numbered option list on the screen → ``(first_line,
    end_line, options)`` with ``options`` as ``[{"num", "label"}]``, or None.

    The list must number 1, 2, 3… with no gaps, hold at least two options,
    and carry one of the CLI's ``cursors`` glyphs on an option line — what
    tells a live selection menu from a numbered list the agent printed. A
    line indented past its option's number that matches no option is that
    option's label wrapping. Below the list only blank lines, rules and
    key-hint footers may follow (a live menu is the last thing on screen; a
    numbered prompt in the transcript has more transcript or the input box
    under it). ``end_line`` is one past the last option line.

    ``widths`` (each line's drawn width, :func:`_drawn_widths`): a label the
    CLI hard-wrapped mid-token (a long path) is joined back with nothing in
    between (:func:`_wrap_joint`); without it, wrapped lines join with a
    space."""
    last = None
    for i in range(len(lines) - 1, -1, -1):
        if _OPTION.match(lines[i]):
            last = i
            break
    if last is None:
        return None
    # Walk up from the last option to the first, absorbing wrapped labels.
    rows: List[Tuple[int, re.Match]] = []
    tails: dict = {}
    pending: List[str] = []
    i = last
    while i >= 0:
        m = _OPTION.match(lines[i])
        if m:
            rows.append((i, m))
            if pending:
                tails[i] = list(reversed(pending))
                pending = []
            if m.group("num") == "1":
                break
            i -= 1
            continue
        text = lines[i]
        if text.strip() and rows and _indent(text) > _label_col(rows[-1][1]) - 3:
            # Indented continuation of the option ABOVE it (Ink hard-wraps).
            pending.append(text.strip())
            i -= 1
            continue
        break
    rows.reverse()
    if len(rows) < 2 or rows[0][1].group("num") != "1":
        return None
    nums = [int(m.group("num")) for _, m in rows]
    if nums != list(range(1, len(rows) + 1)):
        return None
    if not any(
        m.group("cursor") and m.group("cursor").strip() in cursors for _, m in rows
    ):
        return None
    if not _only_footer_below(lines[last + 1 :], _label_col(rows[-1][1])):
        return None
    cols = max(widths, default=0) if widths else 0
    options = []
    for idx, m in rows:
        label = m.group("label")
        prev = (label, widths[idx]) if cols else None
        for k, tail in enumerate(tails.get(idx, []), 1):
            joint = _wrap_joint(prev, tail, cols) if prev else None
            label += (" " if joint is None else joint) + tail
            prev = (tail, widths[idx + k]) if cols else None
        options.append({"num": m.group("num"), "label": label})
    return rows[0][0], last + 1, options


def _only_footer_below(below: Sequence[str], label_col: int) -> bool:
    """Whether the lines under an option list are what a live menu has under
    it: the last option's wrapped label (indented, right under it), blank
    lines, rules and key-hint footers — nothing else."""
    gap = False
    for text in below:
        if not text.strip() or _ANY_RULE.match(text):
            gap = True
            continue
        if not gap and _indent(text) > label_col - 3:
            continue
        stripped = text.strip()
        if _FOOTER.search(stripped) and len(stripped) <= _FOOTER_MAX_CHARS:
            gap = True
            continue
        return False
    return True


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _label_col(m: re.Match) -> int:
    return m.start("label")


def clean_label(label: str) -> str:
    """An option label without its key hint ("(esc)", "(y)") or squashed
    whitespace, capped."""
    return _cap(_KEY_HINT.sub("", _squash(label)), MAX_LABEL_CHARS) or ""


def option_kind(label: str) -> str:
    """``yes`` / ``always`` / ``no`` / ``other`` for one option label."""
    low = _squash(label).lower().replace("’", "'")
    first = re.split(r"[\s,.:;]+", low, maxsplit=1)[0] if low else ""
    if first in _NO_WORDS:
        return "no"
    if first in _YES_WORDS:
        return "always" if any(h in low for h in _ALWAYS_HINTS) else "yes"
    return "other"


def _options(raw: List[dict]) -> List[dict]:
    out = []
    for opt in raw:
        label = clean_label(opt["label"])
        out.append({"key": opt["num"], "label": label, "kind": option_kind(label)})
    return out


def _paragraphs(lines: Sequence[str]) -> List[List[str]]:
    """Blank-line separated groups of stripped, non-rule lines."""
    paras: List[List[str]] = []
    cur: List[str] = []
    for line in lines:
        text = line.strip()
        if not text or _ANY_RULE.match(line):
            if cur:
                paras.append(cur)
                cur = []
            continue
        cur.append(text)
    if cur:
        paras.append(cur)
    return paras


def _first_question(text: str) -> str:
    """``text`` up to and including its first ``?`` (all of it when none)."""
    cut = text.find("?")
    return text[: cut + 1] if cut >= 0 else text


def compose_question(parsed: dict) -> str:
    """One self-contained line for the UI: ``"<heading> — <detail>.
    <question>"`` when the dialog has a heading, ``"<question> <detail>"``
    otherwise (Codex puts its reason after the question)."""
    heading = (parsed.get("heading") or "").strip().rstrip(":")
    detail = (parsed.get("detail") or "").strip()
    question = (parsed.get("question") or "").strip()
    if heading:
        head = heading + (" — " + detail.rstrip(".") if detail else "")
        joiner = " " if head.endswith(("?", "!", ".")) else ". "
        text = head + joiner + question if question else head
    else:
        text = question + (" " + detail if detail else "")
    return _cap(_squash(text), MAX_QUESTION_CHARS) or ""


def _region_key(lines: Sequence[str]) -> str:
    """Screen lines as a cursor-free, whitespace-squashed block: what stays
    the same while the same dialog is up, whichever option is highlighted.
    (The unparsed fallback's key: without a parse there is no telling a
    wrapped line from two lines.)"""
    out = []
    for ln in lines:
        text = _squash(_CURSOR_GLYPHS.sub(" ", ln))
        if text and not _ANY_RULE.match(text):
            out.append(text)
    return "\n".join(out)


def _id_part(lines: Sequence[str], prefix: bool = False) -> str:
    """One id component: ``lines`` cursor-free, with every whitespace
    character and any "(ctrl+o to expand …)" hint removed.

    Cut — a line ends in "…" — or ``prefix`` (a component the width may cut
    even where this screen shows it whole): only the text before the cut,
    capped at :data:`ID_PREFIX_CHARS`. That prefix is what every width
    shows, so the component reads the same wherever the CLI cut it."""
    kept: List[str] = []
    for ln in lines:
        if _TRUNCATED.search(ln):
            kept.append(_TRUNCATED.sub("", ln))
            prefix = True
            break
        kept.append(ln)
    text = _EXPAND_HINT_KEY.sub("", _WS.sub("", _CURSOR_GLYPHS.sub("", "".join(kept))))
    return text[:ID_PREFIX_CHARS] if prefix else text


def _id_key(lines: Sequence[str], options: Sequence[dict], capped=None) -> str:
    """A parsed dialog as its width-independent id key: one line per
    component — each paragraph above the options (lines between blanks and
    rules: the heading, the command box, the question), then each option as
    ``key:label`` — every one built by :func:`_id_part`.

    Re-wrapping at another width only moves line breaks and indentation
    inside a component, so the same dialog gives the same key at 47 columns
    and at 200. An option label is always a prefix (Claude cuts "don't ask
    again … in ~/.mindflock/work…" to the width, and at a wider one shows it
    whole), and so is a paragraph ``capped(first_line)`` names (a collapsed
    block, cut at SOME width). The rest is kept whole: a different command,
    tool or argument is a different prompt. The option KEYS always count —
    the 2-option variant Claude draws in a narrow pane is a different
    dialog to press digits into."""
    out: List[str] = []
    group: List[str] = []

    def flush() -> None:
        if group:
            whole = capped is not None and capped(group[0].strip())
            part = _id_part(group, prefix=bool(whole))
            if part:
                out.append(part)
            group.clear()

    for ln in lines:
        if not ln.strip() or _ANY_RULE.match(ln):
            flush()
            continue
        group.append(ln)
    flush()
    for opt in options:
        out.append("%s:%s" % (opt["num"], _id_part([opt["label"]], prefix=True)))
    return "\n".join(out)


def dialog_id(parsed: Optional[dict], screen: str = "") -> str:
    """A short id that stays the same while the SAME dialog is up.

    The digest of the dialog's screen region (a parse's ``region``: its
    heading down to its last option), else — unparsed — of the screen's last
    15 non-empty lines; either way with the selection cursor removed, so
    arrowing through the options keeps the id. A different dialog (the next
    command, another file, another diff) gets a different id, which is what
    lets ``/answer`` refuse a click meant for the dialog that was there a
    moment ago."""
    if parsed is not None and parsed.get("region"):
        key = str(parsed["region"])
    elif parsed is not None:
        key = "\n".join(
            [
                parsed.get("heading") or "",
                parsed.get("question") or "",
                parsed.get("command") or "",
                parsed.get("detail") or "",
            ]
            + [
                "%s:%s" % (o.get("key"), o.get("label"))
                for o in parsed.get("options") or ()
            ]
        )
    else:
        tail = [ln for ln in normalize_lines(screen) if ln.strip()][-15:]
        key = _region_key(tail)
    digest = hashlib.sha256(key.encode("utf-8", "replace"))
    return digest.hexdigest()[:12]


def fallback_question(screen: str) -> str:
    """Best effort at "what is it asking" for a dialog no parser knows: the
    last line near the bottom with a ``?`` in it, else the last non-empty
    line."""
    lines = [ln.strip() for ln in normalize_lines(screen) if ln.strip()]
    lines = [ln for ln in lines if not _ANY_RULE.match(ln)][-25:]
    for line in reversed(lines):
        if "?" in line:
            return _cap(_squash(line), MAX_QUESTION_CHARS) or ""
    return _cap(_squash(lines[-1]), MAX_QUESTION_CHARS) if lines else ""


def _is_top_rule(line: str) -> bool:
    return bool(_SOLID_RULE.match(line) or _TEXT_RULE.search(line))


def _region(
    lines: Sequence[str], first: int, *, require_rule: bool = False
) -> Optional[List[str]]:
    """The lines above an option block that can belong to its dialog, cut at
    the nearest rule (a dialog's top edge — a solid one, or one with text
    drawn on it).

    ``require_rule`` (a CLI that always draws one): the rule must be there,
    anywhere above the options, or this is no dialog (None) — never a run of
    transcript standing in for the heading. Otherwise up to
    :data:`_REGION_LINES` lines above the options, cut at a rule when there
    is one."""
    start = 0 if require_rule else max(0, first - _REGION_LINES)
    region = list(lines[start:first])
    for i in range(len(region) - 1, -1, -1):
        if _is_top_rule(region[i]):
            return region[i + 1 :]
    return None if require_rule else region


# --------------------------------------------------------------------------- #
# Claude Code
# --------------------------------------------------------------------------- #
#: Claude Code's selection cursor. Never ">": that is how its transcript
#: marks the user's own prompts (and its input box).
CLAUDE_CURSORS = "❯"
#: Dialog headings whose first body line is the thing being approved.
_CLAUDE_COMMAND_HEADINGS = ("bash command", "tool use", "powershell command")


#: A tab header on a dialog's heading line: Claude Code 2.x queues prompts
#: from background sub-agents as tabs — "Tool use · from the general-purpose
#: agent", with "2 of 3" right-aligned when several are queued. The count
#: changes as the other tabs are answered and its position with the width;
#: neither is part of the question.
_TAB_FROM = re.compile(r"^(?P<head>.+?)\s+·\s+from\s+the\s+(?P<src>.+?)\s*$")
_TAB_COUNT = re.compile(r"^(?P<head>.+?)\s+\d+\s+of\s+\d+$")
#: The paragraph Claude Code 2.x draws under an MCP tool's arguments ("About
#: the mindflock — Spawn worker session Tool:" over the tool's description,
#: collapsed to fit the width; in a narrow pane the header line itself wraps).
_ABOUT_TOOL = re.compile(r"^About the \S")
#: An MCP tool line: "mindflock — Spawn worker session Tool: (MCP)".
_TOOL_SUFFIX = re.compile(r"\s*(?:Tool:)?\s*\(MCP\)\s*$")
#: One argument of a tool call as Claude lists it ("title: \"api\"").
_TOOL_ARG = re.compile(r"^(?P<name>[A-Za-z_][\w-]*):(?:\s+(?P<value>.*))?$")


def _split_tab_header(line: str) -> Tuple[str, Optional[str]]:
    """``(heading, source)`` for a heading line that may carry a tab header."""
    text = _squash(line)
    m = _TAB_COUNT.match(text)
    if m:
        text = m.group("head")
    m = _TAB_FROM.match(text)
    if m:
        src = _TAB_COUNT.sub(lambda mm: mm.group("head"), m.group("src"))
        return m.group("head"), src
    return text, None


#: A boxed section's line with its drawn width: ``(text, width)`` — the text
#: with its border removed, the width the screen line had with it.
Row = Tuple[str, int]


def _dashed_sections(rows: Sequence[Row]) -> List[List[Row]]:
    """``rows`` split at Claude Code 2.x's dashed rules: the heading block,
    then each boxed section (a command, a diff, a tool's arguments)."""
    out: List[List[Row]] = [[]]
    for row in rows:
        if _DASHED_RULE.match(row[0]):
            out.append([])
        else:
            out[-1].append(row)
    return out


def _wrap_joint(prev: Row, text: str, cols: int) -> Optional[str]:
    """What joins ``text`` onto the line before it when the CLI wrapped
    that line to fit ``cols`` columns, or None for a line break of the
    text's own.

    Claude Code's boxes stop one column short of the pane's edge and wrap
    like wrap-ansi: only a token longer than the whole line is cut
    mid-token, where the line is full — an EMPTY joint; a full line whose
    last token, glued to the next line's first, would have fitted on a
    line is a word wrap that happened to fill it — one space. A line that
    stops short of the edge ends where the text has a line break (a word
    that would not fit is ambiguous with a real break, and a real break
    shown as a space would hide a heredoc's lines). A continuation never
    starts with blanks (the wrap drops them), so an indented line is the
    text's own. ``cols`` 0 (unknown) never joins."""
    ptext, pwidth = prev
    if not cols or not ptext.strip() or not text.strip() or text[:1].isspace():
        return None
    edge = cols - 1
    if pwidth < edge:
        return None
    inner = edge - (pwidth - len(ptext))
    glued = len(ptext.rsplit(None, 1)[-1]) + len(text.split(None, 1)[0])
    return "" if glued > inner else " "


def _block_text(rows: Sequence[Row], cols: int = 0) -> str:
    """A boxed section's rows as text: dedented, inner blank lines kept,
    the blank ones around it dropped, and the lines the box wrapped to fit
    ``cols`` columns joined back (see :func:`_wrap_joint`) — a long command
    is ONE line, whatever the pane's width; its own line breaks stay."""
    rows = [(t.rstrip(), w) for t, w in rows]
    while rows and not rows[0][0].strip():
        rows.pop(0)
    while rows and not rows[-1][0].strip():
        rows.pop()
    indent = min((_indent(t) for t, _ in rows if t.strip()), default=0)
    out = ""
    for i, (text, _) in enumerate(rows):
        text = text[indent:]
        if i:
            joint = _wrap_joint((rows[i - 1][0][indent:], rows[i - 1][1]), text, cols)
            out += "\n" if joint is None else joint
        out += text
    return out


def _tool_args(rows: Sequence[Row], cols: int = 0) -> List[Tuple[str, str]]:
    """A tool call's arguments as ``[(name, value)]`` from its argument box:
    ``name: value`` lines, a value on the lines under ``name:`` (a long
    string, drawn in a box of its own) joined onto it — with nothing in
    between where the box hard-wrapped a token (see :func:`_wrap_joint`)."""
    args: List[Tuple[str, str]] = []
    prev: Optional[Row] = None
    for line, width in rows:
        text = line.strip()
        if not text:
            prev = None
            continue
        m = _TOOL_ARG.match(text)
        if m:
            args.append((m.group("name"), (m.group("value") or "").strip()))
        elif args:
            name, value = args[-1]
            joint = _wrap_joint(prev, text, cols) if prev and value else None
            args[-1] = (
                name,
                (value + (" " if joint is None else joint) + text).strip(),
            )
        prev = (line, width)
    return args


def _unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


#: The argument a Tool use dialog's command line leads with, by NAME in
#: this order (Claude lists a call's arguments in the order the agent wrote
#: them — spawn_session's ``prompt`` often before its ``title``), else the
#: first argument that has a value.
_LEAD_ARGS = ("title", "session", "to", "target")


def _lead_arg(args: Sequence[Tuple[str, str]]) -> str:
    """The value a Tool use dialog's command line leads with (unquoted)."""
    by_name: dict = {}
    for name, value in args:
        by_name.setdefault(name, value)
    for name in _LEAD_ARGS:
        if by_name.get(name):
            return _unquote(by_name[name])
    return next((_unquote(v) for _, v in args if v), "")


def _claude_v2_command(
    heading: str, sections: List[List[Row]], cols: int = 0
) -> Tuple[Optional[str], Optional[str]]:
    """``(command, detail)`` for a Claude Code 2.x Bash / Tool use dialog,
    whose body is split by dashed rules (``cols``: the screen's width, to
    tell the box's wrapping from the command's own line breaks).

    * Bash: the heading block holds the DESCRIPTION ("Add and commit
      bye.txt"), the first box the command itself (its own lines kept, the
      box's wrapping joined back).
    * Tool use (an MCP tool): the heading block holds the tool ("mindflock —
      Spawn worker session Tool: (MCP)"), the first box its arguments. The
      command line LEADS with the argument that says which call this is,
      chosen by name (:data:`_LEAD_ARGS`: the worker's title for
      spawn_session, the session for answer_prompt or stop_session) —
      because the rail has a row's width to show it in and the tool name
      alone says nothing. The arguments go to ``detail``."""
    head_rest = [t.strip() for t, _ in sections[0] if t.strip()][1:]
    box = sections[1] if len(sections) > 1 else []
    if heading.lower().startswith("tool use"):
        tool = _TOOL_SUFFIX.sub("", " ".join(head_rest)).strip() or None
        args = _tool_args(box, cols)
        lead = _lead_arg(args)
        command = " · ".join(p for p in (lead, tool) if p) or None
        detail = ", ".join("%s: %s" % (n, v) if v else n for n, v in args) or None
        return command, detail
    description = " ".join(head_rest) or None
    command = _block_text(box, cols) or None
    return command, description


def parse_claude(screen: str) -> Optional[dict]:
    """A Claude Code dialog (a top rule, the heading, the body, the question,
    ``❯ 1.`` options, a key-hint footer) → the parsed dict, or None.

    * Bash / Tool use (an MCP tool), 2.x layout (dashed rules split the
      body): see :func:`_claude_v2_command` — the command is the boxed
      command line, its description goes to the question; a tool call leads
      with its first argument.
    * Bash / Tool use, earlier layout: the body's first paragraph is the
      command (``uv add redis`` / ``mindflock - spawn_session(…) (MCP)``,
      over several lines when Ink wrapped it) over its one-line description.
    * Edit / Create / Write file: the body's first line is the path; the
      diff under it is skipped.
    * Folder trust ("Accessing workspace:"): the path is the detail, the
      question is the "Quick safety check" sentence.
    * Anything else with numbered options (plan approval, AskUserQuestion):
      the question paragraph and the options.

    A tab header on the heading ("· from the general-purpose agent 2 of 3",
    a background sub-agent's prompt) is cut off the heading and reported as
    ``source``.
    """
    lines = normalize_lines(screen)
    widths = _drawn_widths(screen)
    block = option_block(lines, CLAUDE_CURSORS, widths)
    if block is None:
        return None
    first, end, raw = block
    above = _region(lines, first, require_rule=True)
    if above is None:
        return None
    above = list(above)
    source = None
    for i, line in enumerate(above):
        if line.strip() and not _ANY_RULE.match(line):
            core, source = _split_tab_header(line)
            above[i] = " " + core
            break
    paras = _paragraphs(above)
    # The question is in the LAST paragraph with a "?" (a command above it
    # may hold one too; the key-hint footer and "Security guide" below never
    # do).
    q_idx = next(
        (i for i in range(len(paras) - 1, -1, -1) if any("?" in ln for ln in paras[i])),
        None,
    )
    if q_idx is None:
        return None
    q_para = paras[q_idx]
    q_line = next(k for k, ln in enumerate(q_para) if "?" in ln)
    # Its sentence starts at the "?" line, or earlier when that line is the
    # hard-wrapped tail of a longer one (it then starts in lower case).
    start = q_line
    while start > 0 and q_para[start][:1].islower():
        start -= 1
    question = _first_question(" ".join(q_para[start : q_line + 1]))
    lead = q_para[:start]
    before = paras[:q_idx]
    # The heading is the dialog's first line; the body is what lies between
    # it and the question, still in paragraphs (Ink hard-wraps a long
    # command, so one command can span several lines of one paragraph).
    if before:
        heading = before[0][0]
        body = [p for p in [before[0][1:]] + before[1:] + [lead] if p]
    elif lead:
        heading, body = lead[0], [lead[1:]] if len(lead) > 1 else []
    else:
        heading, body = None, []
    command = detail = None
    low = (heading or "").lower()
    top = first - len(above)
    sections = _dashed_sections(list(zip(above, widths[top:first])))
    if low.startswith(_CLAUDE_COMMAND_HEADINGS) and len(sections) >= 3:
        command, detail = _claude_v2_command(
            heading or "", sections, max(widths, default=0)
        )
    elif low.startswith(_CLAUDE_COMMAND_HEADINGS) and body:
        # "uv add redis" over "Add redis as a dependency": the paragraph's
        # last line is the description, the lines above it the command.
        para = body[0]
        if len(para) > 1:
            command, detail = " ".join(para[:-1]), para[-1]
        else:
            command = para[0]
            detail = body[1][0] if len(body) > 1 else None
    elif body and not body[0][0].endswith(":"):
        # A file dialog's path, the trust dialog's folder, a description —
        # not a lead-in ("Here is Claude's plan:") to text it leaves out.
        detail = body[0][0]
    return {
        "heading": _cap(heading, MAX_DETAIL_CHARS),
        "question": _cap(_squash(question), MAX_QUESTION_CHARS),
        "command": _cap(command, MAX_COMMAND_CHARS),
        "detail": _cap(_squash(detail) if detail else None, MAX_DETAIL_CHARS),
        "options": _options(raw),
        "source": source,
        "region": _id_key(above, raw, capped=lambda ln: bool(_ABOUT_TOOL.match(ln))),
    }


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #
#: Codex's selection cursor.
CODEX_CURSORS = "›>"
#: The lines that open a Codex dialog (codex-cli 0.146 approval overlay,
#: permission request, user-input request and folder-trust screen).
_CODEX_OPENERS = re.compile(
    r"^(?:Would you like to .*\?|Do you trust the contents of this directory\?"
    r"|Do you want to .*\?|.* needs your approval\.?)"
)


def parse_codex(screen: str) -> Optional[dict]:
    """A Codex dialog (the bottom-pane approval overlay: the question, an
    optional ``Reason:`` line, the ``$ command`` or the edit summary, ``›
    1.`` options, a key-hint footer) → the parsed dict, or None.

    Codex draws no rule above its dialog, so the dialog starts at the
    nearest line that OPENS one ("Would you like to run the following
    command?", "Do you trust the contents of this directory?", …); without
    one, the nearest paragraph with a question mark stands in."""
    lines = normalize_lines(screen)
    block = option_block(lines, CODEX_CURSORS)
    if block is None:
        return None
    first, end, raw = block
    region = [ln.strip() for ln in _region(lines, first) or ()]
    start = None
    for i in range(len(region) - 1, -1, -1):
        if _CODEX_OPENERS.match(region[i]):
            start = i
            break
    if start is None:
        for i in range(len(region) - 1, -1, -1):
            if "?" in region[i]:
                start = i
                break
    if start is None:
        return None
    question = _first_question(region[start])
    command = detail = None
    cmd_lines: List[str] = []
    for text in region[start + 1 :]:
        if not text or _ANY_RULE.match(text):
            if cmd_lines:
                break
            continue
        if text.startswith("$ "):
            cmd_lines.append(text[2:].strip())
        elif cmd_lines:
            cmd_lines.append(text)  # a multi-line command's continuation
        elif text.lower().startswith("reason:") and detail is None:
            detail = text
        elif detail is None and not question.startswith("Do you trust"):
            detail = text
    if cmd_lines:
        command = "\n".join(cmd_lines)
    return {
        "heading": None,
        "question": _cap(_squash(question), MAX_QUESTION_CHARS),
        "command": _cap(command, MAX_COMMAND_CHARS),
        "detail": _cap(_squash(detail) if detail else None, MAX_DETAIL_CHARS),
        "options": _options(raw),
        "source": None,
        "region": _id_key(region[start:], raw),
    }


# --------------------------------------------------------------------------- #
# The /dialog payload
# --------------------------------------------------------------------------- #
def describe(parsed: Optional[dict], screen: str) -> dict:
    """``GET /api/instances/{title}/dialog``'s body for one screen and the
    provider's parse of it (None when it had none).

    Parsed: the composed question line, the command and the dialog's own
    options. Unparsed: ``parsed: false``, the best-effort question line and
    no options — the UI then offers only "open the pane". The ``id`` is
    :func:`dialog_id` either way."""
    if parsed is not None and not parsed.get("options"):
        parsed = None
    if parsed is None:
        return {
            "id": dialog_id(None, screen),
            "parsed": False,
            "question": fallback_question(screen),
            "command": None,
            "options": [],
        }
    body = {
        "id": dialog_id(parsed, screen),
        "parsed": True,
        "question": compose_question(parsed),
        "command": parsed.get("command") or None,
        "options": [
            {"key": str(o["key"]), "label": str(o["label"]), "kind": str(o["kind"])}
            for o in parsed["options"]
        ],
    }
    if parsed.get("source"):
        # Emitted only when set: who raised it ("general-purpose agent" — a
        # background sub-agent's prompt), from the tab header the question
        # no longer carries.
        body["source"] = str(parsed["source"])
    return body


# --------------------------------------------------------------------------- #
# Screen evidence for the typing guards
# --------------------------------------------------------------------------- #
#: How many non-empty lines up from the bottom a dialog's own phrases are
#: looked for: a live dialog is the last thing on the screen, and phrases
#: further up are transcript (an answered prompt, an agent quoting one).
SCREEN_BOTTOM_LINES = 15


def screen_bottom(screen: str, n: int = SCREEN_BOTTOM_LINES) -> str:
    """The last ``n`` non-empty lines of ``screen`` (borders and a side panel
    removed), as text."""
    lines = [ln for ln in normalize_lines(screen) if ln.strip()]
    return "\n".join(lines[-n:])


def dialog_on_screen(
    screen: str,
    parsed: Optional[dict],
    waiting: Sequence[str] = (),
    trust: Sequence[str] = (),
) -> bool:
    """Whether ``screen`` shows a live dialog — the evidence every automated
    typer checks right before it types (see ``BaseProvider.dialog_on_screen``).

    A parse with options says so outright. Without one (a layout the parser
    does not know, a redraw glitch, a side panel it could not cut away), the
    provider's waiting-prompt regexes and trust-gate phrases still count when
    they match in the bottom :data:`SCREEN_BOTTOM_LINES` lines. Deliberately
    looser than the parse: a false "dialog" only HOLDS a message for the next
    pass, while a missed one types it into the prompt — and its Enter picks
    the highlighted option."""
    if parsed is not None and parsed.get("options"):
        return True
    tail = screen_bottom(screen)
    if not tail:
        return False
    for pat in waiting or ():
        try:
            if re.search(pat, tail):
                return True
        except re.error:
            if pat in tail:
                return True
    return any(p and p in tail for p in trust or ())
