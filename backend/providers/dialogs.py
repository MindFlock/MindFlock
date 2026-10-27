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
the layouts: Claude Code's (:func:`parse_claude`), Codex's
(:func:`parse_codex`), and — read by :class:`GenericProvider
<backend.providers.generic.GenericProvider>` through its config's
``dialog_layout`` (:data:`LAYOUTS`) — aider's, opencode's, Cline's, goose's
and Antigravity's. All are pinned against golden screens under
``tests/unit/data/dialogs/``.

A parse is deliberately conservative. It needs the marks of a LIVE prompt
at the bottom of the screen: a numbered option list with the CLI's selection
cursor on one of its lines (only blank lines, rules and a key-hint footer
below it), or the CLI's own equivalent — aider's ``[Yes]:`` input line as
the last line, opencode's button row with its ``⇆ select`` hint, Cline's
``[y] Approve  [n] Deny`` row, goose's ``●``/``○`` radio list under ``◆``.
An agent PRINTING a numbered list never carries those marks, and a numbered
prompt the user typed sits in the transcript, above the input box. Anything
else returns None, and the UI falls back to the question line plus a button
that opens the pane — a wrong button is worse than no button.

The screen may carry the terminal's colour escapes (``capture-pane -e``:
what ``/dialog`` reads, see ``agent_io.current_dialog``): every parser
strips them (:func:`strip_ansi`), and opencode — whose selected button is
marked by its background colour alone — reads its selection from them
(:func:`_highlighted`). Without them a parse assumes the CLI's initial
selection.

A parsed dialog (``parse_*``'s return)::

    {"heading": str|None,   # the dialog's title line ("Bash command")
     "question": str,       # the question it asks ("Do you want to proceed?")
     "command": str|None,   # what it asks about, when that is a command
     "detail": str|None,    # one line of context (a description, a path)
     "options": [{"key": "1", "label": "Yes", "kind": "yes"}, ...],
                            # + "keys": ["Down", "Enter"] — the tmux keys
                            # that pick it — where pressing ``key`` alone
                            # is not how the CLI is answered (a cursor-
                            # driven list, aider's "y" + Enter)
     "source": str|None,    # who asked, from a tab header ("general-purpose
                            # agent" for a background sub-agent's prompt)
     "region": str}         # what the id hashes: one line per component
                            # (a paragraph, each option), cursor-free, with
                            # ALL whitespace removed and anything the width
                            # can cut reduced to a short prefix

Claude Code's question tool (AskUserQuestion, :func:`_claude_ask`) parses to
the same dict plus ``"kind": "select"``, ``"multi"`` (checkboxes), ``"steps"``
(the tab row: ``[{"label", "done"}]``, the review tab ``"submit": True``),
``"step"`` (the active tab), ``"review"`` (the review screen's ``[{"question",
"answer"}]``, else None), ``"cursor"`` (the key under the cursor),
``"button"`` (a multi-select's own "Next" / "Submit" row) and ``"nav"``
(the keys that switch tabs / press the cursor's row); each option adds
``"desc"`` (its description, None when it only repeats the label) and
``"checked"`` (None without a checkbox), and its ``kind`` is ``other`` (a
choice), ``text`` (the "Type something" row), ``chat`` ("Chat about this"),
or ``yes`` / ``no`` (Submit answers / Cancel).

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
import json
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
    "strip_ansi",
    "parse_claude",
    "parse_codex",
    "parse_aider",
    "parse_opencode",
    "parse_cline",
    "parse_goose",
    "parse_antigravity",
    "LAYOUTS",
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
#: First words that are a standing approval by themselves ("Always allow",
#: goose's "Always Allow").
_ALWAYS_WORDS = ("always",)

# A terminal escape sequence: CSI (colours, cursor moves), OSC (titles,
# hyperlinks) or a two-byte escape. ``capture-pane -e`` emits SGR colours.
_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]"
)


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


def strip_ansi(text: str) -> str:
    """``text`` without terminal escape sequences (``capture-pane -e``)."""
    return _ANSI.sub("", text or "")


def _raw_lines(screen: str) -> List[str]:
    """The screen's lines as captured — escapes kept — index for index with
    :func:`normalize_lines`."""
    return (screen or "").replace("\r", "").split("\n")


def _styled(raw: str) -> Tuple[str, List[Optional[str]]]:
    """One captured line → ``(text, backgrounds)``: the text without its
    escapes, and per character the background it was drawn on (an SGR
    colour spec such as ``"5;215"``, the foreground's under reverse video,
    None for the default)."""
    text: List[str] = []
    backs: List[Optional[str]] = []
    fg = bg = None
    rev = False
    pos = 0
    for m in _ANSI.finditer(raw or ""):
        for ch in raw[pos : m.start()]:
            text.append(ch)
            backs.append(fg if rev else bg)
        pos = m.end()
        seq = m.group(0)
        if not (seq.startswith("\x1b[") and seq.endswith("m")):
            continue
        params = seq[2:-1].replace(":", ";").split(";")
        i = 0
        while i < len(params):
            p = params[i]
            if p in ("", "0"):
                fg = bg = None
                rev = False
            elif p == "7":
                rev = True
            elif p == "27":
                rev = False
            elif p in ("38", "48"):
                if i + 1 < len(params) and params[i + 1] == "5":
                    spec, i = ";".join(params[i + 1 : i + 3]), i + 2
                elif i + 1 < len(params) and params[i + 1] == "2":
                    spec, i = ";".join(params[i + 1 : i + 5]), i + 4
                else:
                    spec = None
                if p == "38":
                    fg = spec
                else:
                    bg = spec
            elif p == "39":
                fg = None
            elif p == "49":
                bg = None
            elif p.isdigit() and (30 <= int(p) <= 37 or 90 <= int(p) <= 97):
                fg = p
            elif p.isdigit() and (40 <= int(p) <= 47 or 100 <= int(p) <= 107):
                bg = p
            i += 1
    for ch in (raw or "")[pos:]:
        text.append(ch)
        backs.append(fg if rev else bg)
    return "".join(text), backs


def _highlighted(raw: str, labels: Sequence[str]) -> Optional[int]:
    """Which of ``labels`` — buttons drawn side by side on the captured line
    ``raw`` — is highlighted, by background colour alone, or None (no
    colours captured, or no single answer).

    A highlighted button's label sits on a background the cell two columns
    to its left (the gap before its padding) does not have; when the theme
    paints every button, the one whose background no other button shares."""
    text, backs = _styled(raw)
    starts: List[int] = []
    pos = 0
    for label in labels:
        at = text.find(label, pos)
        if at < 0:
            return None
        starts.append(at)
        pos = at + len(label)
    if not any(backs):
        return None
    own = [backs[s] for s in starts]
    lit = [i for i, s in enumerate(starts) if s >= 2 and own[i] != backs[s - 2]]
    if len(lit) == 1:
        return lit[0]
    if len(labels) >= 3:
        unique = [i for i, b in enumerate(own) if own.count(b) == 1]
        if len(unique) == 1:
            return unique[0]
    return None


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
    raws = [raw.rstrip() for raw in strip_ansi(screen).replace("\r", "").split("\n")]
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
    raws = [raw.rstrip() for raw in strip_ansi(screen).replace("\r", "").split("\n")]
    return [len(raw.rstrip()) for raw in _strip_side_panel(raws)]


def option_block(
    lines: Sequence[str], cursors: str, widths: Optional[Sequence[int]] = None
) -> Optional[Tuple[int, int, List[dict]]]:
    """The LAST numbered option list on the screen → ``(first_line,
    end_line, options)`` with ``options`` as ``[{"num", "label"}]``, or None.

    The list must number 1, 2, 3… with no gaps, hold at least two options,
    and carry one of the CLI's ``cursors`` glyphs on an option line (the
    option it is on gets ``"cursor": True``) — what
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
        cursor = bool(m.group("cursor") and m.group("cursor").strip() in cursors)
        options.append({"num": m.group("num"), "label": label, "cursor": cursor})
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
    if first in _ALWAYS_WORDS:
        return "always"
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


# --- AskUserQuestion (Claude Code 2.1.311) ---------------------------------- #
#: The question tool's tab row, right under the dialog's top rule: one tab per
#: question (☐ unanswered, ☒ answered) and the review tab (✔ Submit), between
#: ← and → arrows when there is more than one tab. The ACTIVE tab is drawn on
#: a background colour alone (``backgroundColor: "permission"``, padded with a
#: space each side); a narrow pane cuts the labels with "…", and the
#: narrowest shows only the active tab's.
_ASK_TAB_ROW = re.compile(r"^\s*(?:←\s+)?(?P<tabs>[☐☒✔]\s.*?)(?:\s+→)?\s*$")
_ASK_TAB = re.compile(r"(?P<mark>[☐☒✔])[ \t]?(?P<label>[^☐☒✔]*)")
#: One choice: the cursor, the number, a checkbox (multi-select), the label.
_ASK_OPTION = re.compile(
    r"^(?P<indent>\s*)(?P<cursor>❯\s*)?(?P<num>[1-9])\.\s+"
    r"(?:(?P<box>\[[ ✔]\])\s+)?(?P<label>\S.*?)\s*$"
)
#: A multi-select question's own button under its choices: "Next", or
#: "Submit" on the last question.
_ASK_BUTTON = re.compile(r"^\s*(?P<cursor>❯\s*)?(?P<label>Submit|Next)\s*$")
#: The free-text choice's placeholder ("Type something." single-select,
#: "[ ] Type something" multi-select); the typed text replaces it.
_ASK_TEXT_LABEL = re.compile(r"^Type something\.?$")
_ASK_REVIEW = "Review your answers"
#: What the key-hint footer wraps onto its second line in a narrow pane.
_ASK_FOOTER_TAIL = re.compile(r"[A-Za-z/↑↓·+ -]{1,40}")
#: The keys that move between the tabs, and that pick the row under the
#: cursor — served so a client never names tmux keys itself.
ASK_NAV = {"next": ["Right"], "prev": ["Left"], "submit": ["Enter"]}


def _ask_tabs(text: str) -> List[dict]:
    """The tab row's text → ``[{"label", "done"}]`` (the review tab adds
    ``"submit": True``)."""
    steps = []
    for m in _ASK_TAB.finditer(text):
        label = _squash(m.group("label"))
        if m.group("mark") == "✔":
            steps.append({"label": label or "Submit", "done": False, "submit": True})
        else:
            steps.append({"label": label, "done": m.group("mark") == "☒"})
    return steps


def _ask_active_tab(raw: str, steps: Sequence[dict]) -> Optional[int]:
    """The tab drawn on a background of its own in the captured tab row
    ``raw`` (colour escapes kept), or None without colours."""
    text, backs = _styled(raw)
    marks = [i for i, ch in enumerate(text) if ch in "☐☒✔"]
    if len(marks) != len(steps) or not any(backs):
        return None
    lit = [k for k, col in enumerate(marks) if backs[col] is not None]
    return lit[0] if len(lit) == 1 else None


def _ask_step(
    steps: Sequence[dict],
    raw: str,
    question: str,
    button: Optional[str],
    review: bool,
) -> int:
    """The active tab's index. From the colours when the capture has them;
    without them: the review tab on the review screen; the last question
    when a multi-select's button says "Submit"; the only tab whose label is
    drawn (the narrowest pane); the tab whose label words the question uses
    ("Toppings" — "Which toppings…"); the first unanswered one."""
    lit = _ask_active_tab(raw, steps)
    if lit is not None:
        return lit
    questions = [k for k, s in enumerate(steps) if not s.get("submit")]
    if review:
        return next((k for k, s in enumerate(steps) if s.get("submit")), len(steps) - 1)
    if len(questions) <= 1:
        return questions[0] if questions else 0
    if button == "Submit":
        return questions[-1]
    drawn = [k for k in questions if steps[k]["label"]]
    if len(drawn) == 1:
        return drawn[0]
    asked = set(re.findall(r"[a-z0-9]+", question.lower()))

    def stem(word: str) -> str:
        return word[:5] if len(word) > 5 else word

    stems = {stem(w) for w in asked}
    hits = [
        k
        for k in questions
        if any(
            stem(w) in stems
            for w in re.findall(r"[a-z0-9]+", steps[k]["label"].lower().rstrip("…"))
            if len(w) >= 3
        )
    ]
    if len(hits) == 1:
        return hits[0]
    return next((k for k in questions if not steps[k]["done"]), questions[0])


def _ask_review(lines: Sequence[str]) -> List[dict]:
    """The review screen's ``● question`` / ``→ answer`` pairs (each may wrap
    onto indented lines; anything else — a "not answered" warning — is
    skipped)."""
    pairs: List[dict] = []
    field = None
    for line in lines:
        text = line.strip()
        if not text:
            field = None
            continue
        if text.startswith("●"):
            pairs.append({"question": text[1:].strip(), "answer": ""})
            field = "question"
        elif text.startswith("→") and pairs:
            pairs[-1]["answer"] = text[1:].strip()
            field = "answer"
        elif field and pairs and _indent(line) >= 2:
            pairs[-1][field] = (pairs[-1][field] + " " + text).strip()
        else:
            field = None
    return [
        {
            "question": _cap(_squash(p["question"]), MAX_QUESTION_CHARS) or "",
            "answer": _cap(_squash(p["answer"]), MAX_DETAIL_CHARS) or "",
        }
        for p in pairs
    ]


def _ask_footer_only(below: Sequence[str]) -> bool:
    """Whether ``below`` (under the choices) holds only blank lines, rules
    and the key-hint footer — which a narrow pane wraps onto a second
    line."""
    footer = False
    for text in below:
        stripped = text.strip()
        if not stripped or _ANY_RULE.match(text):
            continue
        if _FOOTER.search(stripped) and len(stripped) <= _FOOTER_MAX_CHARS:
            footer = True
            continue
        if footer and _ASK_FOOTER_TAIL.fullmatch(stripped):
            continue  # the footer's wrapped tail ("cancel")
        return False
    return True


def _claude_ask(
    lines: Sequence[str], raws: Sequence[str], widths: Sequence[int]
) -> Optional[dict]:
    """Claude Code's AskUserQuestion prompt → the parsed dict (``kind:
    "select"``), or None when the screen does not end in one.

    The layout, under the dialog's top rule: the tab row, the question, the
    numbered choices — each label over its description, a ``[ ]`` / ``[✔]``
    checkbox in front when several may be picked, the free-text "Type
    something" choice last — then a rule and "Chat about this", and the
    key-hint footer. A multi-select question draws its own Next / Submit
    button under its choices (not a choice). The review tab shows "Review
    your answers", each question with its ``→`` answer, "Ready to submit
    your answers?" and Submit answers / Cancel."""
    tab = None
    for i in range(len(lines) - 1, -1, -1):
        if _ASK_TAB_ROW.match(lines[i]):
            above = next((ln for ln in reversed(lines[:i]) if ln.strip()), "")
            if _SOLID_RULE.match(above):
                tab = i
            break
    if tab is None:
        return None
    steps = _ask_tabs(_ASK_TAB_ROW.match(lines[tab]).group("tabs"))
    if not steps:
        return None
    body = list(lines[tab + 1 :])
    first = next((k for k, ln in enumerate(body) if _ASK_OPTION.match(ln)), None)
    if first is None:
        return None
    paras = _paragraphs(body[:first])
    review = bool(paras) and paras[0] == [_ASK_REVIEW]
    if review:
        q_lines = paras[-1] if len(paras) > 1 else []
        rest: List[List[str]] = []
    else:
        q_lines = paras[0] if paras else []
        rest = paras[1:]
    question = _squash(" ".join(q_lines))
    if not question:
        return None
    cols = max(widths, default=0)
    opts: List[dict] = []
    button = None
    button_cursor = False
    rule = False
    k = first
    while k < len(body):
        line = body[k]
        m = _ASK_OPTION.match(line)
        if m:
            opts.append(
                {
                    "num": m.group("num"),
                    "label": [m.group("label")],
                    "desc": [],
                    "box": m.group("box"),
                    "cursor": bool(m.group("cursor")),
                    "col": m.start("label"),
                    "width": widths[tab + 1 + k] if tab + 1 + k < len(widths) else 0,
                    "below_rule": rule,
                }
            )
            k += 1
            continue
        if not line.strip():
            break
        if _ANY_RULE.match(line):
            rule = True
            k += 1
            continue
        b = _ASK_BUTTON.match(line)
        if b and opts[-1]["box"] and not rule and _indent(line) < opts[-1]["col"] - 1:
            button = b.group("label")
            button_cursor = bool(b.group("cursor"))
            k += 1
            continue
        opt = opts[-1]
        if _indent(line) < opt["col"] - 1:
            return None  # neither a choice nor its label's or description's line
        text = line.strip()
        # A label line that ran to the pane's edge wrapped onto this one;
        # one that stopped short ended, and this is its description.
        if not opt["desc"] and cols and opt["width"]:
            token = len(text.split(None, 1)[0])
            if opt["width"] + 1 + token > cols - 1:
                opt["label"].append(text)
                opt["width"] = widths[tab + 1 + k] if tab + 1 + k < len(widths) else 0
                k += 1
                continue
        opt["desc"].append(text)
        k += 1
    if not _ask_footer_only(body[k:]):
        return None
    nums = [int(o["num"]) for o in opts]
    if len(opts) < 2 or nums != list(range(1, len(opts) + 1)):
        return None
    if not (any(o["cursor"] for o in opts) or button_cursor):
        return None
    multi = any(o["box"] for o in opts)
    above_rule = [o for o in opts if not o["below_rule"]]
    text_num = None
    if not review:
        for o in above_rule:
            if _ASK_TEXT_LABEL.match(_squash(" ".join(o["label"]))):
                text_num = o["num"]
        if text_num is None and len(above_rule) < len(opts) and len(above_rule) >= 2:
            text_num = above_rule[-1]["num"]
    # The free-text row under the cursor holds the keyboard: a digit typed
    # there is text, so every other choice is reached by leaving it (Up).
    typing = any(o["cursor"] and o["num"] == text_num for o in opts)
    options = []
    cursor = None
    for o in opts:
        label = clean_label(" ".join(o["label"]))
        if review:
            kind = "yes" if label.lower().startswith("submit") else option_kind(label)
        elif o["below_rule"]:
            kind = "chat"
        elif o["num"] == text_num:
            kind = "text"
        else:
            kind = "other"
        desc = _cap(_squash(" ".join(o["desc"])), MAX_DETAIL_CHARS) or None
        if desc is not None and _squash(desc).lower() == _squash(label).lower():
            desc = None
        keys = [o["num"]] if (not typing or o["num"] == text_num) else ["Up", o["num"]]
        options.append(
            {
                "key": o["num"],
                "label": label,
                "kind": kind,
                "keys": keys,
                "desc": desc,
                "checked": (o["box"] == "[✔]") if o["box"] else None,
            }
        )
        if o["cursor"]:
            cursor = o["num"]
    step = _ask_step(
        steps, raws[tab] if tab < len(raws) else "", question, button, review
    )
    # The id: the question (and the review's answers), then each choice as
    # key:label+description with the checkbox and cursor gone — ticking boxes
    # and moving the cursor keep it; the free-text choice counts by its key
    # alone, so typing into it keeps it too. The tab row is left out: its
    # ticks change as questions are answered and its labels with the width.
    region = ["ask:review" if review else "ask"]
    if review:
        for p in _ask_review(body[:first]):
            region.append(_WS.sub("", p["question"] + "→" + p["answer"]))
    region += [_id_part(p) for p in [q_lines] + rest]
    for o in opts:
        if o["num"] == text_num:
            region.append("%s:text" % o["num"])
        else:
            region.append("%s:%s" % (o["num"], _id_part(o["label"] + o["desc"], True)))
    detail = _squash(" ".join(" ".join(p) for p in rest)) or None
    return {
        "heading": _cap(steps[step]["label"] or None, MAX_DETAIL_CHARS),
        "question": _cap(question, MAX_QUESTION_CHARS),
        "command": None,
        "detail": _cap(detail, MAX_DETAIL_CHARS),
        "options": options,
        "source": None,
        "region": "\n".join(region),
        "kind": "select",
        "multi": multi,
        "steps": steps,
        "step": step,
        "review": _ask_review(body[:first]) if review else None,
        "cursor": cursor,
        "button": button,
        "nav": {k: list(v) for k, v in ASK_NAV.items()},
    }


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

    The question tool's own layout (2.1.311: a tab row, choices with
    descriptions, checkboxes, the review screen) is :func:`_claude_ask`'s —
    a ``kind: "select"`` parse.
    """
    if is_claude_menu(screen):
        return None
    lines = normalize_lines(screen)
    widths = _drawn_widths(screen)
    ask = _claude_ask(lines, _raw_lines(screen), widths)
    if ask is not None:
        return ask
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
# "Update available · 0.146.0 → 0.164.0" is Codex's self-update picker
# (Update now / Skip / Skip until next version): no question mark, so it
# needs its own opener. Unparsed, it read as idle, and a dialog-safe send
# pressed Enter on "Update now" — which updated the user's Codex (2026-10-23).
_CODEX_OPENERS = re.compile(
    r"^(?:Would you like to .*\?|Do you trust the contents of this directory\?"
    r"|Do you want to .*\?|.* needs your approval\.?|Update available\b.*)"
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
# Cursor-driven and hotkey layouts (aider, opencode, Cline, goose, Antigravity)
# --------------------------------------------------------------------------- #
def _option(key: str, label: str, kind: str, keys: Sequence[str]) -> dict:
    return {"key": key, "label": label, "kind": kind, "keys": list(keys)}


def _move_keys(cursor: int, target: int, back: str, forward: str) -> List[str]:
    """The keys that move a selection from ``cursor`` to ``target`` without
    wrapping round the list's end, then press Enter."""
    step = forward if target > cursor else back
    return [step] * abs(target - cursor) + ["Enter"]


def _keyed(raw: List[dict]) -> List[dict]:
    """:func:`option_block` options as ``{"num", "label"}`` for
    :func:`_id_key` (it hashes ``num:label``)."""
    return [{"num": o["key"], "label": o["label"]} for o in raw]


# --- aider ----------------------------------------------------------------- #
#: aider's confirm prompt (``io.confirm_ask``, aider 0.86): the question, then
#: `` (Y)es/(N)o`` with optional ``/(A)ll``, ``/(S)kip all`` and ``/(D)on't
#: ask again``, then the default (`` [Yes]: ``) — an input line, so the
#: prompt is the screen's LAST line while aider waits.
_AIDER_PROMPT = re.compile(
    r"^(?P<q>\S.*?)\s?(?P<opts>\(Y\)es/\(N\)o(?:/\([ASD]\)[^/\[]*?)*)"
    r"\s?\[(?P<default>[^\]]*)\]:$"
)
#: Each choice's label and kind, by its letter. "(D)on't ask again" REFUSES
#: and stops asking (``confirm_ask`` returns False and remembers the
#: question); "(A)ll" says yes to the rest of the group; "(S)kip all" no.
_AIDER_CHOICES = {
    "Y": ("Yes", "yes"),
    "N": ("No", "no"),
    "A": ("All", "always"),
    "S": ("Skip all", "no"),
    "D": ("Don't ask again", "no"),
}
#: The questions aider asks about a SUBJECT it prints on the line(s) above
#: (bold): a URL, a file, a path, the shell command(s).
_AIDER_SUBJECT = re.compile(
    r"^(?:Add (?:URL|file) to the chat|Create new file|Run shell commands?"
    r"|Run pip install|Allow edits to file that has not been added to the chat"
    r"|Open (?:documentation )?url for more info)\?$",
    re.IGNORECASE,
)
_AIDER_COMMAND_SUBJECT = re.compile(r"^Run (?:shell commands?|pip install)\?$")
#: A line that is no subject: an answered prompt, the user's input line.
_AIDER_NOT_SUBJECT = re.compile(r"\(Y\)es/\(N\)o|\]:\s*\S{0,20}$|^\w*>\s")
_AIDER_MAX_WRAP = 6
_AIDER_MAX_SUBJECT = 12


def _aider_join(lines: Sequence[str], width: int) -> str:
    """Lines prompt_toolkit / rich folded at the pane's edge, joined back:
    a fold cuts mid-word, so nothing goes in between — except the one
    trailing blank a fold right after a space lost to the right-trim."""
    out = ""
    for i, ln in enumerate(lines):
        if i and width and len(lines[i - 1]) == width - 1:
            out += " "
        out += ln
    return out


def parse_aider(screen: str) -> Optional[dict]:
    """An aider confirm prompt (``Add URL to the chat? (Y)es/(N)o/(A)ll/(S)kip
    all/(D)on't ask again [Yes]:`` as the screen's last line, the subject it
    asks about on the line(s) above) → the parsed dict, or None.

    The prompt wraps at the pane's edge without a line break tmux can join
    (prompt_toolkit draws it), so the last lines are glued back until they
    read as one prompt starting with a capital letter. Each option is
    answered by its letter and Enter (``keys``: ``["y", "Enter"]``)."""
    lines = normalize_lines(screen)
    if not lines or not lines[-1].rstrip().endswith("]:"):
        return None
    width = max(_drawn_widths(screen), default=0)
    bottom = len(lines) - 1
    found = None
    for k in range(1, _AIDER_MAX_WRAP + 1):
        top = bottom - k + 1
        if top < 0 or not lines[top].strip():
            break
        chunk = lines[top : bottom + 1]
        if k > 2 and len({len(ln) for ln in chunk[:-1]}) != 1:
            break  # folded lines are all as wide as the pane
        m = _AIDER_PROMPT.match(_aider_join(chunk, width).strip())
        if m and m.group("q")[:1].isupper():
            found = (top, m)
            break
    if found is None:
        return None
    top, m = found
    question = _squash(m.group("q"))
    letters = re.findall(r"\(([A-Z])\)", m.group("opts"))
    if letters[:2] != ["Y", "N"] or len(set(letters)) != len(letters):
        return None
    if any(ch not in _AIDER_CHOICES for ch in letters):
        return None
    options = [
        _option(ch, _AIDER_CHOICES[ch][0], _AIDER_CHOICES[ch][1], [ch.lower(), "Enter"])
        for ch in letters
    ]
    subject: List[str] = []
    if _AIDER_SUBJECT.match(question):
        i = top - 1
        # A resize redraws the prompt under the copy it failed to erase.
        while i >= 0 and lines[i] in lines[top:]:
            i -= 1
        while i >= 0 and len(subject) < _AIDER_MAX_SUBJECT:
            text = lines[i]
            if (
                not text.strip()
                or _ANY_RULE.match(text)
                or _AIDER_NOT_SUBJECT.search(text)
            ):
                break
            subject.insert(0, text.rstrip())
            i -= 1
    command = detail = None
    if subject and _AIDER_COMMAND_SUBJECT.match(question):
        # One command per line; a line as wide as the pane folds onto the next.
        cmds: List[List[str]] = []
        for ln in subject:
            if cmds and width and len(cmds[-1][-1]) >= width - 1:
                cmds[-1].append(ln)
            else:
                cmds.append([ln])
        command = "\n".join(_aider_join(c, width).strip() for c in cmds)
    elif subject:
        # One item (a URL, a path): the last line, and the full-width lines
        # above it it folded from.
        keep = [subject[-1]]
        for ln in reversed(subject[:-1]):
            if not (width and len(ln) >= width - 1):
                break
            keep.insert(0, ln)
        subject = keep
        detail = _aider_join(keep, width).strip()
    return {
        "heading": None,
        "question": _cap(question, MAX_QUESTION_CHARS),
        "command": _cap(command, MAX_COMMAND_CHARS),
        "detail": _cap(_squash(detail) if detail else None, MAX_DETAIL_CHARS),
        "options": options,
        "source": None,
        "region": _id_key(subject + [""] + [question], _keyed(options)),
    }


# --- opencode -------------------------------------------------------------- #
#: opencode's (1.17) permission prompt draws its options as a row of buttons
#: — "Allow once   Allow always   Reject", and "Confirm   Cancel" on the
#: "Always allow" step that follows "Allow always" — with the key hint
#: ("⇆ select  enter confirm") at the row's end, or under it in a narrow pane.
_OPENCODE_BUTTONS = (("Allow once", "Allow always", "Reject"), ("Confirm", "Cancel"))
_OPENCODE_ROW = re.compile(
    r"^\s*(?P<row>Allow once\s{2,}Allow always\s{2,}Reject|Confirm\s{2,}Cancel)"
    r"(?:\s{2,}(?P<hint>\S.*))?$"
)
_OPENCODE_SELECT_HINT = re.compile(r"⇆\s*select")
#: Each button's kind. "Confirm" commits an "Always allow" rule; "Cancel"
#: only goes back to the permission prompt.
_OPENCODE_KINDS = {
    "Allow once": "yes",
    "Allow always": "always",
    "Reject": "no",
    "Confirm": "always",
    "Cancel": "other",
}
#: The button Esc presses (the prompt's ``escapeKey``) — whatever is selected.
_OPENCODE_ESCAPE = ("Reject", "Cancel")
_OPENCODE_HEADING = re.compile(r"^\s*△\s+(?P<title>\S.*?)\s*$")
#: The question tool's footer (one question, one answer): digits pick.
_OPENCODE_QUESTION_FOOTER = re.compile(
    r"^\s*↑↓\s*select\s+enter\s+\w+\s+esc\s+dismiss\s*$"
)
_OPENCODE_QOPTION = re.compile(
    r"^(?P<indent>\s*)(?P<num>[1-9])\.\s+(?P<label>\S.*?)\s*$"
)
_OPENCODE_MULTI = "(select all that apply)"


def _opencode_permission(screen: str, lines: List[str]) -> Optional[dict]:
    row_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if _OPENCODE_ROW.match(lines[i]):
            row_idx = i
            break
        if lines[i].strip() and not _OPENCODE_SELECT_HINT.search(lines[i]):
            return None  # something else is under the row: no live prompt
    if row_idx is None:
        return None
    m = _OPENCODE_ROW.match(lines[row_idx])
    hinted = bool(m.group("hint") and _OPENCODE_SELECT_HINT.search(m.group("hint")))
    hinted = hinted or any(
        _OPENCODE_SELECT_HINT.search(t) for t in lines[row_idx + 1 :]
    )
    if not hinted:
        return None
    labels = next(b for b in _OPENCODE_BUTTONS if m.group("row").startswith(b[0]))
    head_idx = None
    for i in range(row_idx - 1, max(-1, row_idx - _REGION_LINES), -1):
        if _OPENCODE_HEADING.match(lines[i]):
            head_idx = i
            break
    if head_idx is None:
        return None
    heading = _OPENCODE_HEADING.match(lines[head_idx]).group("title")
    body = [ln.strip() for ln in lines[head_idx + 1 : row_idx]]
    command = detail = None
    if labels[0] == "Confirm":
        # "This will allow the following patterns …" over "- git log *" lines.
        detail = next((t for t in body if t and not t.startswith("- ")), None)
        patterns = [t[2:].strip() for t in body if t.startswith("- ")]
        command = "\n".join(patterns) or None
    else:
        # The tool line ("# Shell command", "→ Edit notes/todo.md"): an icon
        # glyph, then the title.
        tool = next((t for t in body if t), "")
        parts = tool.split(None, 1)
        if len(parts) == 2 and len(parts[0]) == 1 and not parts[0].isalnum():
            tool = parts[1]
        detail = tool or None
        cmd: List[str] = []
        for t in body:
            if t.startswith("$ ") and not cmd:
                cmd.append(t[2:].strip())
            elif cmd and t:
                cmd.append(t)
            elif cmd:
                break
        command = "\n".join(cmd) or None
    sel = _highlighted(_raw_lines(screen)[row_idx], labels)
    cursor = 0 if sel is None else sel
    options = []
    for j, label in enumerate(labels):
        keys = (
            ["Escape"]
            if label in _OPENCODE_ESCAPE
            else _move_keys(cursor, j, "Left", "Right")
        )
        options.append(_option(str(j + 1), label, _OPENCODE_KINDS[label], keys))
    return {
        "heading": _cap(heading, MAX_DETAIL_CHARS),
        "question": "",
        "command": _cap(command, MAX_COMMAND_CHARS),
        "detail": _cap(_squash(detail) if detail else None, MAX_DETAIL_CHARS),
        "options": options,
        "source": None,
        "region": _id_key(lines[head_idx:row_idx], _keyed(options)),
    }


def _opencode_question(lines: List[str]) -> Optional[dict]:
    if not lines or not _OPENCODE_QUESTION_FOOTER.match(lines[-1]):
        return None
    # The block of lines over the footer: "1." … options at one indent, each
    # with its description indented under it.
    end = len(lines) - 1
    while end and not lines[end - 1].strip():
        end -= 1
    i = end - 1
    while i >= 0 and lines[i].strip():
        i -= 1
    block = lines[i + 1 : end]
    first = _OPENCODE_QOPTION.match(block[0]) if block else None
    if first is None:
        return None
    rows: List[Tuple[int, re.Match]] = []
    for k, line in enumerate(block):
        m = _OPENCODE_QOPTION.match(line)
        if m and m.group("indent") == first.group("indent"):
            rows.append((k, m))
        elif _indent(line) <= len(first.group("indent")):
            return None  # neither an option nor its description
    nums = [int(m.group("num")) for _, m in rows]
    if len(rows) < 2 or nums != list(range(1, len(rows) + 1)):
        return None
    if i < 0 or lines[i].strip():
        return None  # the question paragraph sits a blank line above
    while i >= 0 and not lines[i].strip():
        i -= 1
    q_lines: List[str] = []
    while i >= 0 and lines[i].strip():
        q_lines.insert(0, lines[i].strip())
        i -= 1
    question = _squash(" ".join(q_lines))
    if not question or _OPENCODE_MULTI in question:
        return None  # several answers toggle with the digits, then confirm
    above = next((lines[k] for k in range(i, -1, -1) if lines[k].strip()), "")
    if above.strip().endswith("Confirm"):
        return None  # a tab row of several questions over a final Confirm
    options = []
    for _, m in rows:
        label = clean_label(m.group("label"))
        options.append(
            _option(m.group("num"), label, option_kind(label), [m.group("num")])
        )
    return {
        "heading": None,
        "question": _cap(question, MAX_QUESTION_CHARS),
        "command": None,
        "detail": None,
        "options": options,
        "source": None,
        "region": _id_key(q_lines, _keyed(options)),
    }


def parse_opencode(screen: str) -> Optional[dict]:
    """An opencode prompt → the parsed dict, or None.

    * The permission prompt ("△ Permission required", the tool line such as
      "# Shell command" or "→ Edit notes/todo.md", a ``$ command`` or a diff,
      then the "Allow once / Allow always / Reject" buttons with the ``⇆
      select`` hint) and its "△ Always allow" step ("Confirm / Cancel", the
      patterns it will allow as ``command``). The selected button is marked
      by colour only: read from the escapes when the screen carries them
      (:func:`_highlighted`), else the first (where opencode opens it).
      ``keys`` move from it with Left/Right, then Enter; Reject / Cancel is
      Esc, whatever is selected.
    * The question tool ("1. …" options with their descriptions under them,
      "↑↓ select  enter submit  esc dismiss" as the last line): a digit
      picks and submits, so ``keys`` is the option's number. "Type your own
      answer" opens opencode's text box (then type the answer and Enter).
      Several questions, or several answers to tick, are not parsed."""
    lines = normalize_lines(screen)
    return _opencode_question(lines) or _opencode_permission(screen, lines)


# --- Cline ----------------------------------------------------------------- #
#: Cline's (3.0) tool approval: "Cline needs permission", "Approve tool
#: call?", the tool, its arguments as JSON, and "[y] Approve   [n] Deny" — y
#: and n answer at once. Its status lines (model and cost, the repo, the
#: auto-approve mode) stay under the dialog.
_CLINE_PERMISSION = "Cline needs permission"
_CLINE_QUESTION = "Cline is asking a question"
_CLINE_BUTTONS = re.compile(r"^\s*\[y\]\s+Approve\s{2,}\[n\]\s+Deny\s*$")
_CLINE_STATUS_LINES = 4
#: A question's choice row: ">" on the selected one, the text two columns in.
_CLINE_ROW = re.compile(r"^  (?P<cursor>>| ) (?P<text>\S.*?)\s*$")
_CLINE_JSON_STR = r'"((?:\\.|[^"\\])*)"'
#: The tools whose ``command`` argument is a shell command (the editor's is
#: its operation: "create", "str_replace").
_CLINE_COMMAND_TOOL = re.compile(r"command|shell|bash|exec|terminal", re.IGNORECASE)


def _json_str(text: str) -> str:
    try:
        return json.loads('"%s"' % text)
    except ValueError:
        return text


def _cline_status_below(below: Sequence[str]) -> bool:
    """Whether ``below`` is only Cline's status lines (the dialog replaced
    the input box; an answered one has the box — a rule and "❯" — under
    it)."""
    rest = [t for t in below if t.strip()]
    return len(rest) <= _CLINE_STATUS_LINES and not any(
        _ANY_RULE.match(t) or t.strip().startswith(("❯", ">")) for t in rest
    )


def _cline_permission(lines: List[str]) -> Optional[dict]:
    row = next(
        (i for i in range(len(lines) - 1, -1, -1) if _CLINE_BUTTONS.match(lines[i])),
        None,
    )
    if row is None or not _cline_status_below(lines[row + 1 :]):
        return None
    head = next(
        (
            i
            for i in range(row - 1, max(-1, row - _REGION_LINES), -1)
            if lines[i].strip() == _CLINE_PERMISSION
        ),
        None,
    )
    if head is None:
        return None
    body = [ln.strip() for ln in lines[head + 1 : row] if ln.strip()]
    question = body[0] if body and body[0].endswith("?") else ""
    rest = body[1:] if question else body
    tool = rest[0] if rest and not rest[0].startswith(("{", '"')) else None
    args = " ".join(rest[1:] if tool else rest)
    command = None
    if tool and _CLINE_COMMAND_TOOL.search(tool):
        m = re.search(r'"commands?"\s*:\s*' + _CLINE_JSON_STR, args)
        if m:
            command = _json_str(m.group(1))
        else:
            m = re.search(r'"commands?"\s*:\s*\[(.*?)\]', args)
            if m:
                found = re.findall(_CLINE_JSON_STR, m.group(1))
                command = "\n".join(_json_str(s) for s in found)
    path = re.search(r'"(?:path|file_path|filePath)"\s*:\s*' + _CLINE_JSON_STR, args)
    detail = " ".join(p for p in (tool, _json_str(path.group(1)) if path else "") if p)
    options = [
        _option("y", "Approve", "yes", ["y"]),
        _option("n", "Deny", "no", ["n"]),
    ]
    return {
        "heading": _CLINE_PERMISSION,
        "question": _cap(question, MAX_QUESTION_CHARS),
        "command": _cap(command or None, MAX_COMMAND_CHARS),
        "detail": _cap(_squash(detail) or None, MAX_DETAIL_CHARS),
        "options": options,
        "source": None,
        "region": _id_key(lines[head:row], _keyed(options)),
    }


def _cline_question(lines: List[str]) -> Optional[dict]:
    head = next(
        (
            i
            for i in range(len(lines) - 1, max(-1, len(lines) - _REGION_LINES), -1)
            if lines[i].strip() == _CLINE_QUESTION
        ),
        None,
    )
    if head is None:
        return None
    # The title, the question and the choices, a blank line between each.
    i = head + 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    q_lines: List[str] = []
    while i < len(lines) and lines[i].strip() and not _CLINE_ROW.match(lines[i]):
        q_lines.append(lines[i].strip())
        i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    rows: List[re.Match] = []
    while i < len(lines) and _CLINE_ROW.match(lines[i]):
        rows.append(_CLINE_ROW.match(lines[i]))
        i += 1
    if not q_lines or len(rows) < 3 or not _cline_status_below(lines[i:]):
        return None
    cursors = [k for k, m in enumerate(rows) if m.group("cursor") == ">"]
    if len(cursors) != 1:
        return None
    cursor = cursors[0]
    # The last row is the free-text one ("Type a response...", or what is
    # being typed): typing any text selects it, so it is no button.
    choices = rows[:-1]
    options = []
    for k, m in enumerate(choices):
        label = clean_label(m.group("text"))
        keys = (
            [str(k + 1)]
            if cursor < len(choices) and k < 9
            else (_move_keys(cursor, k, "Up", "Down"))
        )
        options.append(_option(str(k + 1), label, option_kind(label), keys))
    question = _squash(" ".join(q_lines))
    return {
        "heading": None,
        "question": _cap(question, MAX_QUESTION_CHARS),
        "command": None,
        "detail": None,
        "options": options,
        "source": None,
        "region": _id_key([_CLINE_QUESTION, ""] + q_lines, _keyed(options)),
    }


def parse_cline(screen: str) -> Optional[dict]:
    """A Cline dialog → the parsed dict, or None.

    * Tool approval ("Cline needs permission" … "[y] Approve   [n] Deny"):
      the tool is the detail (with the path it names), a ``command`` /
      ``commands`` argument the command; ``keys`` ``["y"]`` / ``["n"]``.
    * A question ("Cline is asking a question", the question, its choices
      with ">" on the selected one, then the free-text row): a digit picks a
      choice (arrows and Enter while the cursor is on the free-text row,
      where a digit would be typed). The free-text row is no option — any
      typed text lands in it, so a written answer is ``/answer`` text plus
      Enter."""
    lines = normalize_lines(screen)
    return _cline_permission(lines) or _cline_question(lines)


# --- goose ----------------------------------------------------------------- #
#: goose's (1.41) tool confirmation: the tool call ("▸ shell" over its
#: arguments) under a rule, "◆  Goose would like to call the above tool, do
#: you allow?", and a radio list — "●" on the selected choice, which also
#: shows its hint "(Allow the tool call once)" — closed by "└". Answered, the
#: "◆" turns "◇" and the list collapses to the choice.
_GOOSE_QUESTION = re.compile(r"^\s*◆\s+(?P<q>\S.*?)\s*$")
_GOOSE_OPTION = re.compile(r"^\s*(?P<mark>[●○])\s+(?P<label>\S.*?)\s*$")
_GOOSE_HINT = re.compile(r"\s*\([^()]*\)$")
_GOOSE_TOOL = re.compile(r"^\s*▸\s+(?P<tool>\S.*?)\s*$")
_GOOSE_ARG = re.compile(r"^\s*(?P<name>[A-Za-z_][\w-]*)(?::\s*|\s+)(?P<value>.*)$")


def parse_goose(screen: str) -> Optional[dict]:
    """A goose tool confirmation → the parsed dict, or None.

    The tool is the heading, its ``command:`` argument the command, its
    first other argument (``path …``) the detail. The selected choice's hint
    is dropped from its label (it moves with the cursor). ``keys`` move from
    the "●" with Up/Down, then Enter."""
    lines = normalize_lines(screen)
    end = len(lines)
    while end and (not lines[end - 1].strip() or lines[end - 1].strip() == "└"):
        end -= 1
    rows: List[Tuple[int, re.Match]] = []
    i = end - 1
    while i >= 0 and _GOOSE_OPTION.match(lines[i]):
        rows.insert(0, (i, _GOOSE_OPTION.match(lines[i])))
        i -= 1
    if len(rows) < 2 or i < 0:
        return None
    qm = _GOOSE_QUESTION.match(lines[i])
    marks = [m.group("mark") for _, m in rows]
    if qm is None or marks.count("●") != 1:
        return None
    cursor = marks.index("●")
    labels = []
    for k, (_, m) in enumerate(rows):
        label = m.group("label")
        if k == cursor:
            label = _GOOSE_HINT.sub("", label)
        labels.append(clean_label(label))
    options = [
        _option(
            str(k + 1), label, option_kind(label), _move_keys(cursor, k, "Up", "Down")
        )
        for k, label in enumerate(labels)
    ]
    above = list(_region(lines, i) or ())
    heading = command = detail = None
    tool_at = next(
        (k for k in range(len(above) - 1, -1, -1) if _GOOSE_TOOL.match(above[k])), None
    )
    block = above[tool_at:] if tool_at is not None else []
    if block:
        heading = _GOOSE_TOOL.match(block[0]).group("tool")
        for ln in block[1:]:
            am = _GOOSE_ARG.match(ln)
            if not am:
                continue
            if am.group("name") == "command" and command is None:
                command = am.group("value").strip() or None
            elif detail is None and am.group("value").strip():
                detail = ln.strip()
    question = qm.group("q")
    return {
        "heading": _cap(heading, MAX_DETAIL_CHARS),
        "question": _cap(_squash(question), MAX_QUESTION_CHARS),
        "command": _cap(command, MAX_COMMAND_CHARS),
        "detail": _cap(_squash(detail) if detail else None, MAX_DETAIL_CHARS),
        "options": options,
        "source": None,
        "region": _id_key(
            block + [""] + [question],
            [{"num": o["key"], "label": o["label"]} for o in options],
        ),
    }


# --- Antigravity ----------------------------------------------------------- #
#: Antigravity's (agy 1.0) selection cursor on a numbered list.
ANTIGRAVITY_CURSORS = ">"
#: The lines that open an agy dialog: the permission prompt ("Requesting
#: permission for:" over the command, "Do you want to proceed?"), a
#: clarifying question ("Question 1/2: …", "↑/↓ Navigate · enter Select ·
#: esc Skip" under its choices) and the trust gate.
_AGY_OPENERS = re.compile(
    r"^(?:Requesting permission for:|Question \d+/\d+:\s*\S.*|Do you want to .*\?"
    r"|Do you trust the contents of this .*\?)"
)
_AGY_PERMISSION = "Requesting permission for:"
_AGY_QUESTION = re.compile(r"^Question \d+/\d+:\s*(?P<q>\S.*)$")
#: The hint under a question that takes a typed answer (no choices).
_AGY_SKIP_HINT = re.compile(r"esc\s+Skip")


def parse_antigravity(screen: str) -> Optional[dict]:
    """An Antigravity (agy) dialog → the parsed dict, or None.

    A numbered list with agy's ">" cursor under one of its openers
    (:data:`_AGY_OPENERS` — ">" alone also starts agy's input line, so the
    opener is required): "Requesting permission for:" gives the command
    (the indented lines under it) and the question ("Do you want to
    proceed?"); "Question 1/1: …" the question. ``keys`` move from the
    cursor with Up/Down, then Enter. A "Question N/N:" with no choices
    drawn (a typed answer) is returned without options — ``/dialog`` serves
    it unparsed, with its question."""
    lines = normalize_lines(screen)
    block = option_block(lines, ANTIGRAVITY_CURSORS)
    if block is None:
        return _antigravity_free_text(lines)
    first, _end, raw = block
    region = [ln.strip() for ln in _region(lines, first) or ()]
    start = next(
        (i for i in range(len(region) - 1, -1, -1) if _AGY_OPENERS.match(region[i])),
        None,
    )
    if start is None:
        return None
    # A permission prompt's opener sits ABOVE its question: start there.
    for i in range(start - 1, -1, -1):
        if region[i] == _AGY_PERMISSION:
            start = i
            break
        if not region[i] and i < start - 1 and not region[i + 1]:
            break
    heading = command = None
    body = region[start:]
    if body[0] == _AGY_PERMISSION:
        heading = "Requesting permission"
        cmd: List[str] = []
        k = 1
        while k < len(body) and body[k] and "?" not in body[k]:
            cmd.append(body[k])
            k += 1
        command = "\n".join(cmd) or None
        q_line = next((t for t in body[k:] if "?" in t), "")
        question = _first_question(q_line)
    else:
        qm = _AGY_QUESTION.match(body[0])
        question = qm.group("q") if qm else _first_question(body[0])
    cursor = next((k for k, o in enumerate(raw) if o.get("cursor")), 0)
    options = []
    for k, opt in enumerate(raw):
        label = clean_label(opt["label"])
        options.append(
            _option(
                opt["num"],
                label,
                option_kind(label),
                _move_keys(cursor, k, "Up", "Down"),
            )
        )
    return {
        "heading": heading,
        "question": _cap(_squash(question), MAX_QUESTION_CHARS),
        "command": _cap(command, MAX_COMMAND_CHARS),
        "detail": None,
        "options": options,
        "source": None,
        "region": _id_key(body, raw),
    }


def _antigravity_free_text(lines: List[str]) -> Optional[dict]:
    """A "Question N/N:" near the bottom with agy's "esc Skip" hint under it
    and no choices: a question answered in free text."""
    tail = [ln.strip() for ln in lines if ln.strip()][-6:]
    q_at = next(
        (k for k in range(len(tail) - 1, -1, -1) if _AGY_QUESTION.match(tail[k])), None
    )
    below = tail[q_at + 1 :] if q_at is not None else []
    if not any(_AGY_SKIP_HINT.search(t) for t in below):
        return None
    if any(_OPTION.match(t) for t in below):
        return None  # choices drawn (an unreadable menu is no free text)
    question = _AGY_QUESTION.match(tail[q_at]).group("q")
    return {
        "heading": None,
        "question": _cap(_squash(question), MAX_QUESTION_CHARS),
        "command": None,
        "detail": None,
        "options": [],
        "source": None,
        "region": _id_key([tail[q_at]], []),
    }


#: ``ProviderConfig.dialog_layout`` → its parser (what
#: :class:`GenericProvider <backend.providers.generic.GenericProvider>`
#: dispatches on).
LAYOUTS = {
    "aider": parse_aider,
    "opencode": parse_opencode,
    "cline": parse_cline,
    "goose": parse_goose,
    "antigravity": parse_antigravity,
}


# --------------------------------------------------------------------------- #
# The /dialog payload
# --------------------------------------------------------------------------- #
def _describe_option(opt: dict) -> dict:
    out = {"key": str(opt["key"]), "label": str(opt["label"]), "kind": str(opt["kind"])}
    if opt.get("keys"):
        out["keys"] = [str(k) for k in opt["keys"]]
    for field in ("desc", "checked"):
        # A select dialog's choices only (absent from every other dialog's).
        if field in opt:
            out[field] = opt[field]
    return out


#: The fields a ``kind: "select"`` parse adds to the body.
_SELECT_FIELDS = ("heading", "multi", "steps", "step", "cursor", "button", "nav")


def describe(parsed: Optional[dict], screen: str) -> dict:
    """``GET /api/instances/{title}/dialog``'s body for one screen and the
    provider's parse of it (None when it had none).

    Parsed: the composed question line, the command and the dialog's own
    options — each with the ``keys`` that pick it when the parse gave them
    (a cursor-driven list; the UI posts ``keys`` when present, else
    ``[key]``). Unparsed: ``parsed: false``, the best-effort question line
    and no options — the UI then offers only "open the pane". A parse with a
    question but no options (a question answered in free text: Antigravity's
    write-in) is unparsed too, with ITS question line. The ``id`` is
    :func:`dialog_id` either way."""
    if parsed is not None and not parsed.get("options"):
        question = compose_question(parsed) if parsed.get("question") else ""
        return {
            "id": dialog_id(parsed if parsed.get("region") else None, screen),
            "parsed": False,
            "question": question or fallback_question(screen),
            "command": None,
            "options": [],
        }
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
        "options": [_describe_option(o) for o in parsed["options"]],
    }
    if parsed.get("source"):
        # Emitted only when set: who raised it ("general-purpose agent" — a
        # background sub-agent's prompt), from the tab header the question
        # no longer carries.
        body["source"] = str(parsed["source"])
    if parsed.get("kind") == "select":
        # Claude's question tool: the question line as asked (its tab label
        # is ``heading``, not glued onto it), the tabs, the review.
        body["kind"] = "select"
        body["question"] = _cap(
            _squash(parsed.get("question") or ""), MAX_QUESTION_CHARS
        )
        if parsed.get("detail"):
            body["detail"] = parsed["detail"]
        for field in _SELECT_FIELDS:
            body[field] = parsed.get(field)
        if parsed.get("review") is not None:
            body["review"] = parsed["review"]
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


#: Claude Code's OWN command menus (/model, /effort, /config, /permissions,
#: /usage, the slash autocomplete): the person opened them, nobody is asking
#: them anything, and the /model picker even draws the same ``❯ 2.`` cursor
#: list a dialog does. The footers (and the picker's heading) name them.
CLAUDE_MENU_SIGNS = (
    r"Enter to set as default",
    r"\bs (?:to use|for) this session only",
    r"Tab to toggle",
    r"←/→ to (?:switch|adjust)",
    r"Type to filter",
    r"\br to retry\b",
    r"^\s*Select model\s*$",
    r"^\s*❯ /[a-z]",  # the slash-command autocomplete's cursor row
)
_CLAUDE_MENU_RX = re.compile("|".join("(?:%s)" % p for p in CLAUDE_MENU_SIGNS), re.M)
#: The same, as the lookahead that keeps the cursor pattern in
#: ``ClaudeProvider.waiting_prompt_patterns`` from matching those menus.
CLAUDE_MENU_VETO = r"(?![\s\S]*(?:to set as default|this session only|Tab to toggle|←/→ to (?:switch|adjust)|Type to filter|\br to retry\b))"


def is_claude_menu(screen: str) -> bool:
    """Whether the visible screen is one of Claude Code's own command menus
    (:data:`CLAUDE_MENU_SIGNS`) rather than a dialog the agent raised."""
    return bool(screen) and _CLAUDE_MENU_RX.search(screen) is not None


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
    if not tail or is_claude_menu(tail):
        return False
    for pat in waiting or ():
        try:
            if re.search(pat, tail):
                return True
        except re.error:
            if pat in tail:
                return True
    return any(p and p in tail for p in trust or ())
