"""Named orchestration prompts ("playbooks") the UI pastes into an agent.

A playbook is a short, plain-language instruction that drives the agent's own
MindFlock MCP tools: "split this across workers", "ask that session", "check
on your workers", "wrap them up". The web UI renders one into text and TYPES
it into the agent's input box without submitting (``/send`` with
``submit: false``); the user adds the task and presses Enter. Nothing runs
until they do, and every step the agent then takes goes through the MCP — so
the report-back footer, the safe-delete checks and the scope limits all still
apply. The New Session dialog's "Split across workers" uses the same registry
to decorate a launch prompt (:func:`decorate_prompt`).

Rules every template keeps (pinned by the tests):

* ONE paragraph of at most :data:`MAX_TEMPLATE_CHARS` characters with its
  arguments empty. Claude Code collapses a longer paste into "[Pasted text]",
  which would hide from the user exactly what they are about to send.
* It names only real ``mindflock`` tools (:data:`TOOL_NAMES`): as
  ``mcp__mindflock__<tool>`` for a Claude agent, the bare tool name for any
  other CLI.
* Rendering is deterministic: the same id, arguments and context always give
  the same text.
* A text argument left empty makes the template END with its lead-in ("The
  task: ") so the user can type straight on from the pasted text.

Pure and stdlib-only, like the rest of :mod:`backend.mcp`: the web server
builds the context (provider, branch, children) and does every lookup.
"""

from __future__ import annotations

import re
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "PLAYBOOKS",
    "IDS",
    "TOOL_NAMES",
    "MAX_TEMPLATE_CHARS",
    "MAX_ARG_CHARS",
    "MAX_SESSION_ARG_CHARS",
    "SPLIT_HEADER",
    "PlaybookError",
    "get",
    "registry",
    "visible",
    "validate_args",
    "render",
    "decorate_prompt",
    "tool_name",
]

#: Claude Code collapses a paste longer than this into "[Pasted text]".
MAX_TEMPLATE_CHARS = 600
#: Ceiling on one free-text argument (a task or a question). The paste path
#: is the user's own words going into their own agent, so this only keeps a
#: runaway client from typing a novel into a terminal.
MAX_ARG_CHARS = 4000
#: Ceiling on a session-title argument. Titles have no length cap of their
#: own (an intake title runs to 120 characters), and the argument is matched
#: EXACTLY against the live sessions — so it is taken as given, never
#: squashed or cut; this only bounds a runaway client.
MAX_SESSION_ARG_CHARS = 256
#: How much of a session title (or a branch) a template quotes — what bounds
#: the length a named session adds to the paste. A longer one is shortened
#: with "…" in the TEXT only.
MAX_SESSION_CHARS = 120
#: A worker list longer than this is summarized as a count in ``wrapup``.
_MAX_NAMES_CHARS = 120

#: Every tool the MindFlock MCP serves (``backend.mcp.tools.build_tools``);
#: a template may name these and nothing else.
TOOL_NAMES = (
    "whoami",
    "list_sessions",
    "get_session",
    "read_output",
    "get_diff",
    "send_message",
    "check_inbox",
    "wait_for_message",
    "report_result",
    "spawn_session",
    "wait_for_session",
    "answer_prompt",
    "kill_session",
    "set_parent",
)

#: First line of a launch prompt decorated with ``split`` — also how
#: :func:`decorate_prompt` recognizes one it already decorated.
SPLIT_HEADER = "Split across workers (MindFlock): "

ARG_KINDS = ("text", "session")
WHENS = ("any", "has_children")


class PlaybookError(ValueError):
    """An unknown playbook id or arguments it does not accept."""


def _arg(name: str, label: str, kind: str, required: bool = False) -> dict:
    return {"name": name, "label": label, "kind": kind, "required": required}


#: The v1 registry, in menu order. ``desc`` may name ``{title}`` (the session
#: the menu was opened on); ``letter`` is the menu's accelerator.
PLAYBOOKS: Tuple[dict, ...] = (
    {
        "id": "split",
        "label": "Split across workers…",
        "desc": (
            "Commit shared groundwork, give each independent piece its own "
            "worker session, wait for their reports"
        ),
        "letter": "S",
        "args": (_arg("task", "Task", "text"),),
        "when": "any",
    },
    {
        "id": "ask",
        "label": "Ask a session…",
        "desc": "Ask another agent; {title} waits for the reply and uses it",
        "letter": "A",
        "args": (
            _arg("session", "Session", "session", required=True),
            _arg("question", "Question", "text"),
        ),
        "when": "any",
    },
    {
        "id": "workers",
        "label": "Check on workers",
        "desc": "One line per worker; answers read-only prompts, flags the rest",
        "letter": "C",
        "args": (),
        "when": "has_children",
    },
    {
        "id": "wrapup",
        "label": "Wrap up workers",
        "desc": (
            "Review each diff, merge, run the tests — asks you before deleting any"
        ),
        "letter": "W",
        "args": (_arg("only", "Only this worker", "session"),),
        "when": "has_children",
    },
)

IDS = tuple(p["id"] for p in PLAYBOOKS)
_BY_ID: Dict[str, dict] = {p["id"]: p for p in PLAYBOOKS}

# Control characters (C0 incl. ESC/CR/LF/TAB, DEL, C1) and bidi overrides:
# typed into a terminal they would submit, escape or reorder the line.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")
_WS_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# Registry views
# --------------------------------------------------------------------------- #
def get(playbook_id: str) -> Optional[dict]:
    """The registry entry for ``playbook_id`` (a copy), or None."""
    pb = _BY_ID.get(playbook_id or "")
    return _public(pb) if pb is not None else None


def _public(pb: dict, title: str = "") -> dict:
    return {
        "id": pb["id"],
        "label": pb["label"],
        "desc": pb["desc"].replace("{title}", title or "this session"),
        "letter": pb["letter"],
        "args": [dict(a) for a in pb["args"]],
        "when": pb["when"],
    }


def registry(title: str = "") -> List[dict]:
    """Every playbook as a JSON-ready dict, in menu order; ``title`` fills the
    descriptions that name the session the menu was opened on."""
    return [_public(pb, title) for pb in PLAYBOOKS]


def visible(playbook_id: str, has_children: bool) -> bool:
    """Whether the playbook belongs in a menu for a session that has (or has
    no) live children — ``has_children`` ones only make sense with workers."""
    pb = _BY_ID.get(playbook_id or "")
    if pb is None:
        return False
    return pb["when"] != "has_children" or bool(has_children)


# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #
def _one_line(text: str) -> str:
    """``text`` as one safe line: control characters gone, every run of
    whitespace (newlines included) a single space. A template is one
    paragraph, and a stray newline pasted into an agent's input could submit
    half of it."""
    return _WS_RE.sub(" ", _CTRL_RE.sub("", str(text))).strip()


def validate_args(playbook_id: str, args) -> dict:
    """``args`` checked against the playbook's declared arguments → the clean
    ``{name: str}`` map (every declared name present, "" when not given).

    Raises :class:`PlaybookError` for an unknown id, a non-object ``args``, an
    argument the playbook does not declare, a non-string value, a missing
    required one, or one over its length ceiling. Text arguments come back as
    one safe line; session arguments come back exactly as given (the caller
    matches them against the live titles, which may hold runs of spaces) —
    whether they name a live session is the caller's question."""
    pb = _BY_ID.get(playbook_id or "")
    if pb is None:
        raise PlaybookError("unknown playbook: %s" % (playbook_id,))
    if args is None:
        args = {}
    if not isinstance(args, Mapping):
        raise PlaybookError("args must be an object")
    declared = {a["name"]: a for a in pb["args"]}
    extra = sorted(str(k) for k in args if k not in declared)
    if extra:
        raise PlaybookError(
            "%s takes no argument %s (it takes: %s)"
            % (
                playbook_id,
                ", ".join(extra),
                ", ".join(declared) or "nothing",
            )
        )
    out: Dict[str, str] = {}
    for name, spec in declared.items():
        raw = args.get(name)
        if raw is None:
            raw = ""
        if not isinstance(raw, str):
            raise PlaybookError("%s must be a string" % name)
        if spec["kind"] == "session":
            value = raw if raw.strip() else ""
            ceiling = MAX_SESSION_ARG_CHARS
        else:
            value = _one_line(raw)
            ceiling = MAX_ARG_CHARS
        if len(value) > ceiling:
            raise PlaybookError("%s is too long (max %d characters)" % (name, ceiling))
        if spec["required"] and not value:
            raise PlaybookError("%s is required" % name)
        out[name] = value
    return out


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def tool_name(tool: str, provider: str = "") -> str:
    """How a ``mindflock`` tool is spelled to this agent's CLI: Claude lists
    MCP tools as ``mcp__mindflock__<tool>`` (and has an unrelated built-in
    ``SendMessage``, so the full name matters); other CLIs by the bare name."""
    if tool not in TOOL_NAMES:
        raise ValueError("not a mindflock tool: %s" % tool)
    if (provider or "").strip().lower() == "claude":
        return "mcp__mindflock__%s" % tool
    return tool


def _quote(title: str) -> str:
    """A session title for a template: control characters gone, line breaks
    as spaces (a paste is one line), double quotes neutralized, and cut at
    :data:`MAX_SESSION_CHARS` with "…" — otherwise as the title is spelled,
    runs of spaces included, so the agent can address it."""
    text = _CTRL_RE.sub("", re.sub(r"[\t\n]", " ", str(title))).replace('"', "'")
    if len(text) > MAX_SESSION_CHARS:
        text = text[: MAX_SESSION_CHARS - 1].rstrip() + "…"
    return '"%s"' % text


def _ctx_children(ctx: Mapping) -> List[Tuple[str, bool]]:
    out: List[Tuple[str, bool]] = []
    for child in ctx.get("children") or ():
        if isinstance(child, Mapping) and child.get("title"):
            out.append((str(child["title"]), bool(child.get("reported"))))
    return out


def _names(titles: Sequence[str]) -> str:
    """``a, b, c`` — or a count once the list would bloat the template."""
    joined = ", ".join(_one_line(t) for t in titles)
    if len(joined) <= _MAX_NAMES_CHARS:
        return joined
    return "%d workers" % len(titles)


def _split_body(t) -> str:
    return (
        "Split this task across MindFlock worker sessions: call %s, commit the "
        "shared groundwork (workers fork from your HEAD), cut the work into "
        "independent pieces with disjoint files, call %s once per piece with a "
        "self-contained prompt, then %s on them all. Answer only read-only "
        "prompts (%s); ask me about the rest. Per report: %s, merge its branch "
        "here, run the tests. Ask me before %s with mode delete."
        % (
            t("whoami"),
            t("spawn_session"),
            t("wait_for_session"),
            t("answer_prompt"),
            t("get_diff"),
            t("kill_session"),
        )
    )


def _render_split(args: dict, ctx: Mapping, t) -> str:
    return "%s The task: %s" % (_split_body(t), args["task"])


def _render_ask(args: dict, ctx: Mapping, t) -> str:
    return (
        "Ask the MindFlock session %s for me: send it the question below with %s "
        "(to that title), then %s from that same session and use its answer to "
        "carry on. If it has not answered within 10 minutes, tell me instead of "
        "waiting longer. The question: %s"
        % (
            _quote(args["session"]),
            t("send_message"),
            t("wait_for_message"),
            args["question"],
        )
    )


def _render_workers(args: dict, ctx: Mapping, t) -> str:
    return (
        "Check on your MindFlock workers: call %s with filter children and give "
        "me one line per worker with its status and what it is doing or "
        "reported. If one is waiting on a prompt, look at it with %s view "
        "screen and answer it with %s only when it is clearly safe and "
        "read-only; flag every other prompt to me. Do not merge, message or "
        "stop anyone." % (t("list_sessions"), t("read_output"), t("answer_prompt"))
    )


def _render_wrapup(args: dict, ctx: Mapping, t) -> str:
    branch = _one_line(ctx.get("branch") or "")
    if not branch or len(branch) > MAX_SESSION_CHARS:
        branch = "your branch"
    if args["only"]:
        return (
            "Wrap up your MindFlock worker %s: read %s, merge its branch into %s "
            "and run the full test suite. Stop and tell me on a conflict or a "
            "failing test. When it is merged and green, ask me before %s with "
            "mode delete."
            % (_quote(args["only"]), t("get_diff"), branch, t("kill_session"))
        )
    reported = [title for title, done in _ctx_children(ctx) if done]
    which = (
        "for each one that reported (%s)" % _names(reported)
        if reported
        else "for each one that has reported"
    )
    return (
        "Wrap up your MindFlock workers: %s read %s, merge its branch into %s "
        "and run the full test suite. Stop and tell me on any conflict or "
        "failing test. When everything is merged and green, ask me before %s "
        "with mode delete." % (which, t("get_diff"), branch, t("kill_session"))
    )


_RENDERERS = {
    "split": _render_split,
    "ask": _render_ask,
    "workers": _render_workers,
    "wrapup": _render_wrapup,
}


def render(playbook_id: str, args=None, ctx: Optional[Mapping] = None) -> str:
    """The prompt text for one playbook, ready to paste.

    ``args`` are validated (:func:`validate_args`; :class:`PlaybookError` on
    anything wrong). ``ctx`` describes the session it is pasted into:
    ``provider`` (how tools are named), ``branch`` (what ``wrapup`` merges
    into) and ``children`` — ``[{"title", "reported"}]``, the live workers and
    whether each has reported. Always one line; an empty trailing text
    argument leaves it ending on its lead-in and one space ("The task: ").
    A text argument that would take the paste past
    :data:`MAX_TEMPLATE_CHARS` is a :class:`PlaybookError` naming the room
    it has."""
    clean = validate_args(playbook_id, args)
    ctx = ctx if isinstance(ctx, Mapping) else {}
    provider = str(ctx.get("provider") or "")

    def t(tool: str) -> str:
        return tool_name(tool, provider)

    renderer = _RENDERERS[playbook_id]
    text = renderer(clean, ctx, t)
    if len(text) > MAX_TEMPLATE_CHARS:
        # The template itself fits (pinned by the tests); a filled-in text
        # argument pushed it over — and Claude Code would hide the whole
        # paste behind "[Pasted text]".
        filled = [
            a["name"]
            for a in _BY_ID[playbook_id]["args"]
            if a["kind"] == "text" and clean[a["name"]]
        ]
        if filled:
            room = MAX_TEMPLATE_CHARS - len(
                renderer(dict(clean, **{n: "" for n in filled}), ctx, t)
            )
            raise PlaybookError(
                "%s is too long to paste: keep it to %d characters (a paste "
                "over %d characters is collapsed into [Pasted text])"
                % (filled[0], max(0, room), MAX_TEMPLATE_CHARS)
            )
    return text


def decorate_prompt(
    playbook_id: str, prompt: str, ctx: Optional[Mapping] = None
) -> str:
    """A launch prompt carrying the playbook — the New Session dialog's
    "Split across workers" (only ``split`` decorates).

    ``SPLIT_HEADER + <task>``, a blank line, then the instructions: the task
    stays the prompt's first line (the line the pane pins above the
    terminal), the instructions follow. Idempotent — a prompt that already
    starts with the header comes back unchanged. Raises
    :class:`PlaybookError` for any other playbook or an empty prompt (there is
    nothing to split)."""
    if playbook_id != "split":
        raise PlaybookError("only the split playbook decorates a new session")
    text = (prompt or "").strip()
    if not text:
        raise PlaybookError("a split needs a task: describe what to split")
    if text.startswith(SPLIT_HEADER):
        return prompt
    ctx = ctx if isinstance(ctx, Mapping) else {}
    provider = str(ctx.get("provider") or "")
    body = _split_body(lambda tool: tool_name(tool, provider))
    return "%s%s\n\n%s" % (SPLIT_HEADER, text, body)
