"""The MindFlock voice assistant: talk to the app itself, not to a session.

``POST /api/voice/assistant`` (routes/voice.py) hands each phrase here;
docs/voice-assistant.md is the user's guide. One turn:

1. **A pending confirmation** (``confirm: {id, yes}``) runs or drops the
   action it asked about (:func:`_confirmed`), held :data:`CONFIRM_TTL_S`.
2. **The instant path** (:func:`instant`): "open intake", "voice settings",
   "switch to the sky", "close this", a command-palette label — a strict
   fuzzy match against the dialogs, the Settings screens and the snapshot's
   commands answers in well under 100 ms with no model at all.
3. **The model**: one WARM long-lived process per server (the provider's
   :meth:`~backend.providers.base.CodingProvider.stream_chat_argv` —
   Claude's ``-p --input-format stream-json``), started lazily or by ``POST
   /api/voice/assistant/warm``, restarted when it dies, made fresh when the
   conversation changes or after :data:`FRESH_AFTER_S` idle, stopped after
   :data:`STOP_AFTER_S` idle. A CLI without that mode answers through its
   :meth:`oneshot_argv` once per turn; none at all → an ``error`` event (the
   instant path still works). It runs in ``commit_message.neutral_cwd()``
   and never under pytest (tests replace :func:`spawn_process`).

The model is given the typed catalog (:mod:`voice_assistant_catalog`) and
the state (the client's snapshot + the server's session list) and answers in
lines, parsed while they stream (:class:`LineParser`, :func:`parse_line`)::

    ACT {"do": "open", "target": "intake"}
    SAY Two sessions need you: docs and parser.
    ASK Which project: sitecheck or MindFlock?
    CONFIRM {"do": "session_action", "session": "docs", "action": "delete"} Delete docs?

Every action is validated (:func:`validate`) before anything happens: an
unknown or malformed one is dropped with an ``error`` event, never guessed
at. Destructive ones (:data:`voice_assistant_catalog.DESTRUCTIVE_SESSION_ACTIONS`,
a click on Delete/Remove/Merge/Discard/Reset) always become a ``confirm``.
UiActions go back to the client as ``act``; ServerActions run here
(:func:`_execute`): ``new_session``, ``send``, ``setting``, ``status``,
``help``. Events, in order, as soon as each line completes: ``act``, ``did``,
``say``, ``ask``, ``confirm``, ``error``, then always ``done {ms, model}``.

Talk policy (``voice.assistant_talk``): the system prompt tells the model
how much to say, and :func:`_echo` drops a SAY that merely narrates an
action taken this turn ("Opening intake.") unless the level is chatty.
"""

from __future__ import annotations

import asyncio
import atexit
import difflib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

from backend.web.core import voice_assistant_catalog as _catalog
from backend.web.core import voice_router as _router

__all__ = [
    "Brain",
    "Context",
    "LineParser",
    "BRAIN",
    "build_system_prompt",
    "build_turn_message",
    "instant",
    "parse_line",
    "spawn_process",
    "talk_level",
    "turn",
    "validate",
]

#: The model when ``voice.assistant_model`` is "".
DEFAULT_MODEL = "claude-haiku-5-5"
#: ``voice.assistant_talk`` levels; "" = :data:`DEFAULT_TALK`.
TALK_LEVELS = ("quiet", "brief", "chatty")
DEFAULT_TALK = "brief"
#: A confirmation is answered within this many seconds or dropped.
CONFIRM_TTL_S = 60.0
#: Idle this long, the warm process is replaced by a fresh one (a new
#: conversation: the client starts one after the same two minutes).
FRESH_AFTER_S = 120.0
#: Idle this long, the process is stopped.
STOP_AFTER_S = 600.0
#: A streaming turn that hasn't finished by then is abandoned (the process
#: is replaced).
TURN_TIMEOUT_S = 30.0
#: Seconds a one-shot turn may take.
ONESHOT_TIMEOUT_S = 45.0
#: The reaper's tick.
_REAP_EVERY_S = 5.0
#: State block caps.
MAX_SESSIONS = 30
MAX_COMMANDS = 300
MAX_CLICKABLES = 150
MAX_PREFS = 60
MAX_REPOS = 15
#: Longest phrase / typed text / spoken line.
TEXT_MAX = 2000
SAY_MAX = 400
#: The instant path's fuzzy floor (difflib ratio) and the margin by which
#: the best target must beat a different second one.
INSTANT_MIN = 0.86
INSTANT_MARGIN = 0.05
#: One-shot conversations keep this many past exchanges.
ONESHOT_HISTORY = 6


class AssistantError(RuntimeError):
    """The assistant's model can't be reached."""


def _under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


# --- settings -----------------------------------------------------------------
def _voice() -> Any:
    try:
        from backend.config.settings import load_settings

        return load_settings().voice
    except Exception:  # noqa: BLE001 — defaults
        return None


def talk_level() -> str:
    """``voice.assistant_talk``, "" and unknown → :data:`DEFAULT_TALK`."""
    v = str(getattr(_voice(), "assistant_talk", "") or "").strip().lower()
    return v if v in TALK_LEVELS else DEFAULT_TALK


def model_name() -> str:
    """``voice.assistant_model``, "" → :data:`DEFAULT_MODEL`."""
    return str(getattr(_voice(), "assistant_model", "") or "").strip() or DEFAULT_MODEL


def assistant_program() -> str:
    """The CLI the assistant asks: ``coding_cli.assistant_provider``, else
    the flock's default — addons/assistant.py ``_assistant_program``'s
    rule."""
    try:
        from backend.config.settings import load_settings

        chosen = (load_settings().coding_cli.assistant_provider or "").strip()
        if chosen:
            return chosen
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.config.program import resolve_default_program

        return resolve_default_program() or "claude"
    except Exception:  # noqa: BLE001
        return "claude"


# --- the prompt -----------------------------------------------------------------
_TALK_RULES = {
    "quiet": (
        "Talk level QUIET: use SAY only to answer a question the person asked, "
        "in one short sentence; never SAY anything about what you did."
    ),
    "brief": (
        "Talk level BRIEF: SAY answers to questions and facts the screen does "
        "not show, in at most two short sentences. Never SAY what you just did "
        "(no 'Opening intake', 'Done', 'Switched'): the screen shows it."
    ),
    "chatty": (
        "Talk level CHATTY: you may add one short friendly SAY after a server "
        "action, but never narrate plain navigation (opening, closing, "
        "switching views or sessions)."
    ),
}

_PROTOCOL = """\
Answer ONLY with lines, each exactly one of:
ACT {json}             do one action from the catalog
SAY <text>             say something to the person (spoken aloud)
ASK <text>             ask the person a short question and wait for the answer
CONFIRM {json} <text>  ask a yes/no question before a destructive action
No other text: no markdown, no code fences, no explanations."""

_RULES = """\
Rules:
- Use only actions, dialogs, screens, session actions and settings from the catalog. Never invent one.
- Several ACT lines may follow each other, in order.
- Replies are heard, not read: plain words, at most two short sentences, no lists, paths or symbols.
- If the request is ambiguous (two sessions or projects could fit, the project or task is missing), ASK one short question instead of guessing.
- Destructive actions ALWAYS go through CONFIRM: session_action delete, close or merge; a click on Delete, Remove, Merge, Discard or Reset.
- Questions about how MindFlock works: ACT help. Questions about what sessions are doing or what needs the person: ACT status (with "session" for one session) unless the state already answers it in a few words.
- To tell a session to do something: ACT send. To start new work: ACT new_session with a known project.
- To change a setting: ACT setting with its path; prefer it to opening Settings. Browser-only preferences in the state: ACT pref.
- "this session" / "it" means the focused session in the state.
- If you cannot do it, SAY so in one short sentence."""

_ACTIONS = """\
Actions (the JSON after ACT or CONFIRM):
{"do":"open","target":"<dialog>"} or {"do":"open","target":"settings:<screen>"}   open a dialog or a Settings screen
{"do":"close"}                                       close the open dialog
{"do":"view","mode":"grid"|"sky"}                    switch the main view
{"do":"focus","session":"<title>"}                   go to a session
{"do":"command","label":"<label>"}                   run a command-palette action (a label listed in the state)
{"do":"session_action","session":"<title>","action":"<id>"}   a session action
{"do":"click","label":"<name>"}                      click a visible control (a label listed in the state)
{"do":"pref","key":"<key>","value":<value>}           set a browser preference listed in the state
{"do":"type","text":"<text>"}                        type into the focused text field
{"do":"new_session","repo":"<project>","prompt":"<task>","program"?:"<cli>"}   start a session
{"do":"send","session":"<title>","text":"<prompt>"}  type a prompt into a session and submit it
{"do":"setting","path":"<group.field>","value":<value>}   change a setting
{"do":"status","session"?:"<title>"}                 what a session is doing (no session: what needs the person)
{"do":"help","question":"<question>"}                answer a question about MindFlock from its docs"""


def _clip(v: Any, n: int = 80) -> str:
    s = " ".join(str(v).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _settings_lines(rows: List[dict]) -> List[str]:
    out = []
    for r in rows:
        opts = (
            " [%s]" % "|".join(o or '""' for o in r["options"])
            if r.get("options")
            else ""
        )
        out.append(
            "%s (%s) %s%s = %s"
            % (
                r["path"],
                r["label"],
                r["type"],
                opts,
                _clip(json.dumps(r.get("value"))),
            )
        )
    return out


def build_system_prompt(talk: str, settings_rows: List[dict]) -> str:
    """The process's system prompt: role, protocol, rules, the talk policy
    and the catalog (with each setting's value as of now — later changes
    ride in each turn's state)."""
    dialogs = ", ".join(
        "%s (%s)" % (d, _catalog.DIALOG_LABELS.get(d, d)) for d in _catalog.OPEN_DIALOGS
    )
    screens = ", ".join(
        "%s (%s)" % (k, v)
        for k, v in _catalog.SETTINGS_SCREENS.items()
        if "settings:" + k in _catalog.open_targets()
    )
    actions = ", ".join("%s (%s)" % (k, v) for k, v in _catalog.SESSION_ACTIONS.items())
    return "\n\n".join(
        (
            "You are MindFlock's voice assistant. MindFlock runs coding-agent "
            "sessions (each a git worktree plus an agent CLI) shown in a grid or "
            "a 'sky' view, with dialogs and Settings. The person talks to you "
            "to drive the app: you map what they say to the actions below.",
            _PROTOCOL,
            _RULES,
            _TALK_RULES.get(talk, _TALK_RULES[DEFAULT_TALK]),
            _ACTIONS,
            "Dialogs: " + dialogs,
            "Settings screens: " + screens,
            "Session actions: " + actions,
            "Settings (path (label) type [options] = value):\n"
            + "\n".join(_settings_lines(settings_rows)),
        )
    )


def _session_line(row: dict, now: float) -> str:
    ss = _router.session_state(row, now)
    word = ss["label"]
    if ss["in_bell"]:
        asked = _router.asking_text(row)
        word = "NEEDS YOU: " + (
            ("asking: " + _clip(asked, 100)) if asked else ss["label"]
        )
    parts = ["- %s [%s]" % (row.get("title"), word)]
    name = row.get("name")
    if name and name != row.get("title"):
        parts.append('called "%s"' % _clip(name, 40))
    if row.get("repo"):
        parts.append("repo=%s" % row.get("repo"))
    head = row.get("task_headline") or row.get("last_prompt")
    if head:
        parts.append('task: "%s"' % _clip(head, 90))
    return " ".join(parts)


def _list(v: Any, n: int) -> List[str]:
    return [
        str(x)
        for x in (v if isinstance(v, list) else [])[:n]
        if isinstance(x, str) and x
    ]


def build_turn_message(
    text: str,
    snapshot: dict,
    rows: List[dict],
    repos: List[dict],
    settings_changed: Dict[str, Any],
    now: Optional[float] = None,
) -> str:
    """One user turn: the compact state block, then what was said."""
    now = time.time() if now is None else now
    snap = snapshot if isinstance(snapshot, dict) else {}
    lines = ["<state>"]
    lines.append(
        "view: %s; dialog: %s; settings screen: %s; focused session: %s"
        % (
            snap.get("view") or "grid",
            snap.get("dialog") or "none",
            snap.get("settings_screen") or "none",
            snap.get("focused") or "none",
        )
    )
    listed = [
        r
        for r in rows
        if isinstance(r, dict) and r.get("title") and not r.get("hidden_by")
    ]
    if listed:
        lines.append("sessions:")
        lines.extend(_session_line(r, now) for r in listed[:MAX_SESSIONS])
        if len(listed) > MAX_SESSIONS:
            lines.append("- …and %d more" % (len(listed) - MAX_SESSIONS))
    else:
        lines.append("sessions: none")
    if repos:
        lines.append(
            "projects: "
            + ", ".join(str(r.get("name")) for r in repos[:MAX_REPOS] if r.get("name"))
        )
    cmds = _list(snap.get("commands"), MAX_COMMANDS)
    if cmds:
        lines.append("commands: " + " | ".join(cmds))
    clicks = _list(snap.get("clickables"), MAX_CLICKABLES)
    if clicks:
        lines.append("clickables: " + " | ".join(clicks))
    prefs = [
        p
        for p in (snap.get("prefs") or [])[:MAX_PREFS]
        if isinstance(p, dict) and p.get("key")
    ]
    if prefs:
        lines.append("prefs:")
        for p in prefs:
            opts = (
                " [%s]" % "|".join(map(str, p["options"]))
                if isinstance(p.get("options"), list)
                else ""
            )
            lines.append(
                "- %s (%s) %s%s = %s"
                % (
                    p["key"],
                    p.get("label") or p["key"],
                    p.get("type") or "",
                    opts,
                    _clip(json.dumps(p.get("value"))),
                )
            )
    if settings_changed:
        lines.append(
            "settings changed since the catalog: "
            + "; ".join(
                "%s = %s" % (k, _clip(json.dumps(v)))
                for k, v in settings_changed.items()
            )
        )
    lines.append("</state>")
    lines.append("The person said: " + str(text).strip())
    return "\n".join(lines)


# --- the output protocol --------------------------------------------------------
class LineParser:
    """Text deltas in, complete lines out (a line is complete at its
    newline; :meth:`flush` hands over the last one at the end of the turn)."""

    def __init__(self) -> None:
        self.buf = ""

    def feed(self, chunk: str) -> List[str]:
        self.buf += chunk or ""
        if "\n" not in self.buf:
            return []
        *done, self.buf = self.buf.split("\n")
        return [d for d in done if d.strip()]

    def flush(self) -> List[str]:
        rest, self.buf = self.buf, ""
        return [rest] if rest.strip() else []


_LEAD_RE = re.compile(r"^\s*(?:[-*•>]+\s*|\d+[.)]\s+|`+)?")
_KIND_RE = re.compile(r"^(ACT|SAY|ASK|CONFIRM)\b\s*:?\s*(.*)$", re.IGNORECASE | re.S)
_DECODER = json.JSONDecoder()


def _json_at(s: str) -> Tuple[Optional[Any], str]:
    """``(value, rest)`` of the JSON object that starts ``s`` (after
    spaces), or ``(None, s)``."""
    s = s.lstrip()
    if not s.startswith("{"):
        return None, s
    try:
        val, end = _DECODER.raw_decode(s)
    except ValueError:
        return None, s
    return val, s[end:].strip()


def parse_line(line: str) -> Optional[dict]:
    """One output line → ``{"kind": "act" | "confirm", "action", "text"?}``,
    ``{"kind": "say" | "ask", "text"}``, ``{"kind": "bad", "text"}`` (a
    protocol line whose JSON doesn't parse) or ``{"kind": "prose", "text"}``
    (anything else); None for a blank line or a code fence."""
    s = str(line or "").strip()
    if not s or s.startswith("```"):
        return None
    s = _LEAD_RE.sub("", s, count=1).strip().strip("`").strip()
    m = _KIND_RE.match(s)
    if m is None:
        return {"kind": "prose", "text": s}
    kind, rest = m.group(1).upper(), m.group(2).strip()
    if kind in ("SAY", "ASK"):
        rest = rest.strip().strip('"').strip()
        return {"kind": kind.lower(), "text": rest} if rest else None
    val, tail = _json_at(rest)
    if not isinstance(val, dict):
        return {"kind": "bad", "text": s}
    if kind == "ACT":
        return {"kind": "act", "action": val}
    return {"kind": "confirm", "action": val, "text": tail.strip().strip('"').strip()}


_ECHO_RE = re.compile(
    r"^(?:(?:ok(?:ay)?|sure|alright|all right|got it|done)[,.!]?\s*)*"
    r"(?:(?:i'?ve |i have |i'?m |i am |i will |i'?ll )?(?:open(?:ing|ed)?|show(?:ing|n)?|clos(?:ing|ed)|"
    r"switch(?:ing|ed)|focus(?:ing|ed)|going|went|here(?:'s| is| are)|sent|send(?:ing)?|"
    r"start(?:ing|ed)|turn(?:ing|ed)|set(?:ting)?|chang(?:ing|ed)|typ(?:ing|ed)|"
    r"tak(?:ing|en) you|bringing|brought|done|all set)\b)",
    re.IGNORECASE,
)


def _echo(text: str) -> bool:
    """Whether a SAY only narrates an action ("Opening intake.", "Done.")."""
    s = str(text or "").strip()
    return bool(s) and (
        bool(_ECHO_RE.match(s))
        or s.lower().rstrip(".!") in ("ok", "okay", "done", "sure")
    )


def _spoken(text: str) -> str:
    from backend.web.core.spoken import strip_markdown

    s = " ".join(strip_markdown(str(text or "")).split())
    if len(s) > SAY_MAX:
        s = s[: SAY_MAX - 1].rsplit(" ", 1)[0] + "…"
    return s


# --- validation -----------------------------------------------------------------
UI_DOS = (
    "open",
    "close",
    "view",
    "focus",
    "command",
    "session_action",
    "click",
    "pref",
    "type",
)
SERVER_DOS = ("new_session", "send", "setting", "status", "help")


def _ci(items: List[str], said: Any) -> Optional[str]:
    """``said`` as one of ``items``: exact, else case/space/ellipsis-blind."""
    s = str(said or "")
    if s in items:
        return s

    def k(x: str) -> str:
        return " ".join(str(x).replace("…", "").replace("...", "").lower().split())

    hits = [i for i in items if k(i) == k(s)]
    return hits[0] if len(hits) == 1 else None


def _resolve_session(
    said: Any, rows: List[dict], snapshot: dict
) -> Tuple[Optional[str], Optional[dict]]:
    """``(title, None)`` or ``(None, problem event)``."""
    name = str(said or "").strip()
    if not name:
        foc = str((snapshot or {}).get("focused") or "")
        if foc:
            return foc, None
        return None, {"type": "ask", "text": "Which session?"}
    titles = [
        str(r.get("title")) for r in rows if isinstance(r, dict) and r.get("title")
    ]
    if name in titles:
        return name, None
    for r in rows:
        if (
            isinstance(r, dict)
            and r.get("name")
            and str(r["name"]).lower() == name.lower()
        ):
            return str(r["title"]), None
    hit, cands = _router.match_session(name, rows)
    if hit is not None:
        return str(hit["title"]), None
    if cands:
        names = [str(c.get("name") or c.get("title")) for c in cands[:4]]
        return None, {
            "type": "ask",
            "text": "Which one: %s?"
            % (
                ", ".join(names[:-1]) + " or " + names[-1]
                if len(names) > 1
                else names[0]
            ),
        }
    return None, {
        "type": "error",
        "text": "There's no session called %s." % _clip(name, 40),
    }


def _pref_value(pref: dict, value: Any) -> Tuple[Any, Optional[str]]:
    kind = pref.get("type")
    if kind == "bool":
        if isinstance(value, str) and value.lower() in ("on", "true", "yes"):
            value = True
        elif isinstance(value, str) and value.lower() in ("off", "false", "no"):
            value = False
        return (
            (value, None)
            if isinstance(value, bool)
            else (value, "That takes on or off.")
        )
    if kind == "select":
        opts = [str(o) for o in pref.get("options") or []]
        hit = _ci(opts, value)
        return (
            (hit, None)
            if hit is not None
            else (value, "That's one of: %s." % ", ".join(opts))
        )
    if kind == "number":
        try:
            return (float(value) if "." in str(value) else int(value)), None
        except (TypeError, ValueError):
            return value, "That takes a number."
    return value, None


def validate(
    action: Any, snapshot: dict, rows: List[dict]
) -> Tuple[Optional[dict], Optional[dict], bool]:
    """``(action, problem, destructive)``: the action normalised (titles
    resolved, labels matched to the snapshot's own spelling), or None with
    the ``error`` / ``ask`` event to emit instead. Nothing outside the
    catalog and the snapshot passes."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    if not isinstance(action, dict):
        return (
            None,
            {"type": "error", "text": "Sorry, I couldn't work out what to do."},
            False,
        )
    do = action.get("do")
    if do not in UI_DOS + SERVER_DOS:
        return (
            None,
            {
                "type": "error",
                "text": "Sorry, I can't do that (%s)." % _clip(do or "no action", 30),
            },
            False,
        )

    def bad(text: str):
        return None, {"type": "error", "text": text}, False

    if do == "open":
        target = str(action.get("target") or "").strip()
        targets = _catalog.open_targets()
        hit = _ci(targets, target)
        if hit is None and target.lower().startswith("settings:"):
            label = target.split(":", 1)[1]
            for k, v in _catalog.SETTINGS_SCREENS.items():
                if "settings:" + k in targets and v.lower() == label.strip().lower():
                    hit = "settings:" + k
        if hit is None:
            return bad("There's no screen called %s." % _clip(target, 40))
        return {"do": "open", "target": hit}, None, False
    if do == "close":
        return {"do": "close"}, None, False
    if do == "view":
        mode = str(action.get("mode") or "").lower()
        if mode not in ("grid", "sky"):
            return bad("The view is the grid or the sky.")
        return {"do": "view", "mode": mode}, None, False
    if do in ("focus", "session_action", "send", "status"):
        title = None
        if do != "status" or action.get("session"):
            title, problem = _resolve_session(action.get("session"), rows, snap)
            if problem is not None:
                return None, problem, False
        if do == "focus":
            return {"do": "focus", "session": title}, None, False
        if do == "status":
            return (
                ({"do": "status", "session": title} if title else {"do": "status"}),
                None,
                False,
            )
        if do == "send":
            text = str(action.get("text") or "").strip()
            if not text or len(text) > TEXT_MAX:
                return bad("What should I tell it?")
            return {"do": "send", "session": title, "text": text}, None, False
        act = _ci(list(_catalog.SESSION_ACTIONS), action.get("action"))
        if act is None:
            return bad(
                "There's no session action called %s." % _clip(action.get("action"), 30)
            )
        out = {"do": "session_action", "session": title, "action": act}
        return out, None, act in _catalog.DESTRUCTIVE_SESSION_ACTIONS
    if do == "command":
        label = _ci(_list(snap.get("commands"), MAX_COMMANDS), action.get("label"))
        if label is None:
            return bad("That command isn't available here.")
        return (
            {"do": "command", "label": label},
            None,
            _catalog.is_destructive_click(label),
        )
    if do == "click":
        label = _ci(_list(snap.get("clickables"), MAX_CLICKABLES), action.get("label"))
        if label is None:
            return bad("I can't see %s on the screen." % _clip(action.get("label"), 40))
        return (
            {"do": "click", "label": label},
            None,
            _catalog.is_destructive_click(label),
        )
    if do == "pref":
        prefs = [
            p for p in snap.get("prefs") or [] if isinstance(p, dict) and p.get("key")
        ]
        pref = next((p for p in prefs if p["key"] == action.get("key")), None)
        if pref is None:
            return bad(
                "There's no preference called %s." % _clip(action.get("key"), 30)
            )
        value, err = _pref_value(pref, action.get("value"))
        if err:
            return bad(err)
        return {"do": "pref", "key": pref["key"], "value": value}, None, False
    if do == "type":
        text = str(action.get("text") or "")
        if not text.strip() or len(text) > TEXT_MAX:
            return bad("What should I type?")
        return {"do": "type", "text": text}, None, False
    if do == "new_session":
        prompt = str(action.get("prompt") or "").strip()
        repo = str(action.get("repo") or "").strip()
        if len(prompt) > TEXT_MAX:
            return bad("That task is too long to say.")
        out = {"do": "new_session", "repo": repo, "prompt": prompt}
        program = str(action.get("program") or "").strip()
        if program:
            try:
                from backend import providers

                known = {p.name for p in providers.all_providers()}
            except Exception:  # noqa: BLE001
                known = set()
            if program not in known:
                return bad("I don't know an agent called %s." % _clip(program, 30))
            out["program"] = program
        if action.get("model"):
            out["model"] = str(action.get("model"))
        return out, None, False
    if do == "setting":
        group, field, value, err = _catalog.check_setting(
            action.get("path"), action.get("value")
        )
        if err:
            return bad(err)
        return (
            {"do": "setting", "path": "%s.%s" % (group, field), "value": value},
            None,
            False,
        )
    # help
    q = " ".join(str(action.get("question") or "").split())[:400]
    if not q:
        return bad("What would you like to know?")
    return {"do": "help", "question": q}, None, False


# --- the instant path -----------------------------------------------------------
_LEADS = ("ok ", "okay ", "hey ", "please ", "can you ", "could you ", "would you ")
_TAILS = (" please", " for me", " now")
_CLOSE_RE = re.compile(
    r"^(?:close|dismiss|exit|hide|shut)(?: (?:this|it|that|the dialog|this dialog|the window"
    r"|the panel|the settings|settings|everything))?$|^go away$|^never ?mind close$"
)
_VIEW_RE = re.compile(
    r"^(?:(?:switch|go|change|flip|move)(?: back)? (?:to|over to) |show(?: me)? |back to |open )?"
    r"(?:the )?(?P<v>grid|sky)(?: view)?$"
)
_OPEN_RE = re.compile(
    r"^(?:open(?: up)?|show(?: me)?|go to|bring up|pull up|take me to|switch to|jump to"
    r"|get me|launch|run) (?:the |my |a )?(?P<x>.+)$"
)
_SUFFIX_RE = re.compile(r" (?:dialog|screen|page|panel|window|tab|menu|pane)$")
_DIALOG_ALIASES: Dict[str, Tuple[str, ...]] = {
    "new-session": ("new session", "new session dialog", "new"),
    "palette": ("command palette", "palette", "commands"),
    "shortcuts": ("keyboard shortcuts", "shortcuts", "hotkeys", "key bindings"),
    "recent": ("recent", "recently closed", "workspaces", "recent sessions"),
    "red-zones": ("red zones", "zones"),
    "todo": ("to do", "todo", "to do list", "todos"),
    "assistant-agent": ("assistant agent", "assistant chat"),
    "schedule": ("schedule", "templates", "scheduled sessions"),
    "prompts": ("prompts", "saved prompts", "prompt library"),
    "intake": ("intake", "tickets", "ticket intake"),
    "verify": ("verify", "checklists", "verify checklists"),
    "customize": ("customize", "customise", "sidebar settings"),
}


def _k(s: str) -> str:
    return _router.norm(str(s).replace("…", " ").replace("&", " and "))


def _best(x: str, cands: List[Tuple[str, dict]]) -> Optional[dict]:
    """The action whose name ``x`` is, at :data:`INSTANT_MIN` or better and
    clear of any different action by :data:`INSTANT_MARGIN`."""
    scored = []
    for name, act in cands:
        n = _k(name)
        if not n:
            continue
        r = 1.0 if n == x else difflib.SequenceMatcher(None, x, n).ratio()
        scored.append((r, act))
    scored.sort(key=lambda t: -t[0])
    if not scored or scored[0][0] < INSTANT_MIN:
        return None
    top = scored[0]
    for r, act in scored[1:]:
        if act != top[1]:
            if top[0] - r < INSTANT_MARGIN:
                return None
            break
    return dict(top[1])


def _open_candidates(snapshot: dict, screens_only: bool) -> List[Tuple[str, dict]]:
    out: List[Tuple[str, dict]] = []
    targets = _catalog.open_targets()
    for k, label in _catalog.SETTINGS_SCREENS.items():
        if "settings:" + k not in targets:
            continue
        act = {"do": "open", "target": "settings:" + k}
        out += [(k, act), (label, act)]
        if not screens_only:
            out += [(label + " settings", act), ("settings " + label, act)]
    if screens_only:
        return out
    for d in _catalog.OPEN_DIALOGS:
        act = {"do": "open", "target": d}
        out.append((d.replace("-", " "), act))
        label = _catalog.DIALOG_LABELS.get(d, "")
        if label:
            out.append((re.sub(r"\s*\(.*\)$", "", label), act))
        out += [(a, act) for a in _DIALOG_ALIASES.get(d, ())]
    # A palette command named like a dialog ("New session…") is that dialog;
    # a destructive one ("Delete session") is the model's, which confirms.
    named = {_k(n) for n, _ in out}
    for label in _list((snapshot or {}).get("commands"), MAX_COMMANDS):
        if _k(label) not in named and not _catalog.is_destructive_click(label):
            out.append((label, {"do": "command", "label": label}))
    return out


def instant(text: str, snapshot: Optional[dict] = None) -> Optional[dict]:
    """The UiAction an obvious navigation phrase is, without a model, or
    None. Strict: the whole phrase, and a near-exact name."""
    s = " " + _k(text) + " "
    for lead in _LEADS:
        if s.startswith(" " + lead):
            s = " " + s[len(lead) + 1 :]
    s = s.strip()
    for tail in _TAILS:
        if s.endswith(tail):
            s = s[: -len(tail)].strip()
    if not s:
        return None
    if s in ("settings", "open settings", "show settings", "go to settings"):
        return {"do": "open", "target": "settings"}
    if _CLOSE_RE.match(s):
        return {"do": "close"}
    m = _VIEW_RE.match(s)
    if m:
        return {"do": "view", "mode": m.group("v")}
    m = _OPEN_RE.match(s)
    if m:
        x = _SUFFIX_RE.sub("", m.group("x")).strip()
    elif s.endswith(" settings") or s.startswith("settings "):
        x = s  # "voice settings": a screen, said bare
    else:
        return None
    screens_only = False
    for pat in (r"^(?P<y>.+) settings$", r"^settings (?:for |on )?(?P<y>.+)$"):
        mm = re.match(pat, x)
        if mm and mm.group("y") not in ("the", "my"):
            got = _best(mm.group("y"), _open_candidates(snapshot or {}, True))
            if got is not None:
                return got
            screens_only = True
    if screens_only:
        return None
    return _best(x, _open_candidates(snapshot or {}, False))


# --- confirmations --------------------------------------------------------------
_PENDING: Dict[str, dict] = {}
_PENDING_LOCK = threading.Lock()


def _hold(action: dict, conversation: str, now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    cid = uuid.uuid4().hex[:12]
    with _PENDING_LOCK:
        for k in [k for k, v in _PENDING.items() if v["expires"] <= now]:
            _PENDING.pop(k, None)
        _PENDING[cid] = {
            "action": action,
            "expires": now + CONFIRM_TTL_S,
            "conversation": conversation,
        }
    return cid


def _take(cid: str, now: Optional[float] = None) -> Optional[dict]:
    now = time.time() if now is None else now
    with _PENDING_LOCK:
        got = _PENDING.pop(str(cid or ""), None)
    if got is None or got["expires"] <= now:
        return None
    return got["action"]


def _confirm_text(action: dict) -> str:
    do = action.get("do")
    if do == "session_action":
        what = _catalog.SESSION_ACTIONS.get(action["action"], action["action"])
        return "%s %s?" % (what, action.get("session"))
    if do in ("click", "command"):
        return "%s?" % action.get("label")
    return "Are you sure?"


# --- the I/O a turn needs -------------------------------------------------------
Rows = Callable[[], Awaitable[List[dict]]]


class Context:
    """What a turn reaches outside itself, injected: the routes build it from
    the request, tests fake it. Every callable is async.

    ``rows()`` → ``GET /api/instances``'s rows; ``repos(rows)`` → the known
    projects (``[{name, path}]``); ``create(plan, program)`` →
    routes.voice ``_create_planned``'s answer (``kind`` "new" or
    "new-failed"); ``send(title, text)`` → ``(status, body)`` of the voice
    ``/send``; ``may_configure()`` → whether this caller may write a guarded
    setting (``auth.may_configure``); ``help(question)`` →
    ``voice_help.answer``; ``write_setting(group, field, value)``."""

    def __init__(
        self,
        *,
        rows: Rows,
        repos: Callable[[List[dict]], Awaitable[List[dict]]],
        create: Callable[[dict, str], Awaitable[dict]],
        send: Callable[[str, str], Awaitable[Tuple[int, dict]]],
        may_configure: Callable[[], Awaitable[bool]],
        help: Optional[Callable[[str], Awaitable[dict]]] = None,
        write_setting: Optional[Callable[[str, str, Any], Awaitable[None]]] = None,
    ) -> None:
        self._rows = rows
        self._repos = repos
        self._rows_got: Optional[List[dict]] = None
        self._repos_got: Optional[List[dict]] = None
        self.create = create
        self.send = send
        self.may_configure = may_configure
        self.help = help or _default_help
        self.write_setting = write_setting or _default_write_setting

    async def rows(self) -> List[dict]:
        if self._rows_got is None:
            got = await self._rows()
            self._rows_got = [r for r in got or () if isinstance(r, dict)]
        return self._rows_got

    async def repos(self) -> List[dict]:
        if self._repos_got is None:
            self._repos_got = list(await self._repos(await self.rows()) or ())
        return self._repos_got


async def _default_help(question: str) -> dict:
    from backend.web.core import voice_help as _voice_help

    return await asyncio.to_thread(_voice_help.answer, question)


def _write_setting(group: str, field: str, value: Any) -> None:
    """Save one setting the way ``POST /api/settings`` does: the write, the
    sync stamp, then the settings hooks (a pipeline reconcile when the save
    moved what the pipeline reads, and the ``settings.synced`` refetch)."""
    from backend.config import settings as store
    from backend.web.core import settings_hooks

    path = "%s.%s" % (group, field)
    try:
        before = settings_hooks.pipeline_signature()
    except Exception:  # noqa: BLE001 — no signature = no reconcile, still saved
        before = None
    store.update_settings(**{group: {field: value}})
    try:
        from backend.web.core import settings_sync

        settings_sync.local_change([path])
    except Exception:  # noqa: BLE001 — a save never fails on the sync stamp
        pass
    settings_hooks.after_settings_change([path], pipeline_before=before)


async def _default_write_setting(group: str, field: str, value: Any) -> None:
    await asyncio.to_thread(_write_setting, group, field, value)


def _guarded(group: str, field: str, value: Any) -> bool:
    """Whether ``POST /api/settings`` would need the owner for this write
    (addons/settings.py ``_guarded_paths``)."""
    try:
        from backend.web.addons.settings import _guarded_paths

        return bool(_guarded_paths({group: {field: value}}))
    except Exception:  # noqa: BLE001 — unknown: treat as guarded
        return True


def _said_value(row: Optional[dict], value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if value == "" and row and row.get("options"):
        return "default"
    return _clip(value, 40)


async def _execute(
    action: dict, ctx: Context, conversation: str
) -> AsyncIterator[dict]:
    """Carry out a validated action: a UiAction is handed back as ``act``;
    a ServerAction runs here."""
    do = action["do"]
    if do in UI_DOS:
        yield {"type": "act", "action": action}
        return
    if do == "help":
        try:
            got = await ctx.help(action["question"])
            yield {"type": "say", "text": _spoken(got.get("say") or "I don't know.")}
        except Exception as err:  # noqa: BLE001
            yield {
                "type": "error",
                "text": "I couldn't look that up: %s" % _clip(err, 80),
            }
        return
    if do == "status":
        rows = await ctx.rows()
        yield {
            "type": "say",
            "text": _spoken(_router.status_say(action.get("session") or "", rows)),
        }
        return
    if do == "setting":
        group, _, field = action["path"].partition(".")
        value = action["value"]
        if _guarded(group, field, value) and not await ctx.may_configure():
            yield {
                "type": "did",
                "action": action,
                "ok": False,
                "text": "Only the owner can change that",
                "detail": {"path": action["path"]},
            }
            yield {
                "type": "error",
                "text": "Only this machine's owner can change that setting.",
            }
            return
        row = next(
            (r for r in _catalog.settings() if r["path"] == action["path"]), None
        )
        try:
            await ctx.write_setting(group, field, value)
        except Exception as err:  # noqa: BLE001
            yield {
                "type": "did",
                "action": action,
                "ok": False,
                "text": "Couldn't save it",
                "detail": {"path": action["path"]},
            }
            yield {"type": "error", "text": "I couldn't save that: %s" % _clip(err, 80)}
            return
        label = row["label"] if row else action["path"]
        yield {
            "type": "did",
            "action": action,
            "ok": True,
            "text": "%s: %s" % (label, _said_value(row, value)),
            "detail": {"path": action["path"], "value": value},
        }
        return
    if do == "send":
        try:
            status, body = await ctx.send(action["session"], action["text"])
        except Exception as err:  # noqa: BLE001
            status, body = 500, {"error": str(err)}
        body = body if isinstance(body, dict) else {}
        ok = status < 300
        if ok:
            text = ("Queued for %s" if body.get("queued") else "Sent to %s") % action[
                "session"
            ]
        elif body.get("in_dialog"):
            text = "%s is showing a dialog; I didn't type into it" % action["session"]
        else:
            text = "Couldn't send to %s: %s" % (
                action["session"],
                _clip(body.get("error") or status, 60),
            )
        yield {
            "type": "did",
            "action": action,
            "ok": ok,
            "text": text,
            "detail": {"title": action["session"], "queued": bool(body.get("queued"))},
        }
        if not ok:
            yield {"type": "error", "text": text + "."}
        return
    # new_session
    repos = await ctx.repos()
    said = _router.norm(action.get("repo") or "")
    repo = _router.match_repo(said.split(), repos) if said else None
    known = [r for r in repos if isinstance(r, dict) and r.get("path")]
    if repo is None and not said and len(known) == 1:
        repo = known[0]
    if repo is None:
        if not known:
            yield {
                "type": "error",
                "text": "I don't know any projects yet: open one from the New dialog first.",
            }
            return
        names = [str(r.get("name")) for r in known[:4]]
        yield {
            "type": "ask",
            "text": "Which project: %s?"
            % (
                ", ".join(names[:-1]) + " or " + names[-1]
                if len(names) > 1
                else names[0]
            ),
        }
        return
    if not action.get("prompt"):
        yield {"type": "ask", "text": "What should it do in %s?" % repo.get("name")}
        return
    path = str(repo["path"])
    repo_name = str(repo.get("name") or "") or path.rstrip("/").rsplit("/", 1)[-1]
    plan = {
        "kind": "new",
        "title_base": _router.title_base("", action["prompt"]),
        "name": "",
        "repo_name": repo_name,
        "repo_path": path,
        "task": action["prompt"],
        # The model the person named ("…with opus"): the New dialog's
        # per-session model pin (session_create's ``profile_model``).
        "model": str(action.get("model") or ""),
    }
    try:
        out = await ctx.create(plan, action.get("program") or "")
    except Exception as err:  # noqa: BLE001
        out = {"kind": "new-failed", "say": "Couldn't start it: %s." % _clip(err, 80)}
    if out.get("kind") == "new":
        title = str(out.get("title") or "")
        yield {
            "type": "did",
            "action": action,
            "ok": True,
            "text": "Started %s in %s" % (title, repo_name),
            "detail": {"title": title, "repo": repo_name},
        }
        return
    reason = str(out.get("say") or "Couldn't start it.")
    yield {
        "type": "did",
        "action": action,
        "ok": False,
        "text": reason.rstrip("."),
        "detail": {"repo": repo_name},
    }
    yield {"type": "error", "text": reason}


# --- the model process ----------------------------------------------------------
def spawn_process(argv: List[str], cwd: str):
    """Start the long-lived chat process (``subprocess.Popen``-like: binary
    ``stdin`` / ``stdout``, ``poll()``, ``kill()``). Never under pytest:
    tests replace this."""
    if _under_pytest():
        raise AssistantError("no model processes under pytest")
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    env.setdefault("TERM", "dumb")
    return subprocess.Popen(
        argv,
        cwd=cwd or None,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
        start_new_session=True,
    )


def run_oneshot(argv: List[str]) -> str:
    """Run a one-shot argv (``commit_message._run``) → stdout. Tests replace
    it; never under pytest."""
    if _under_pytest():
        raise AssistantError("no model calls under pytest")
    from backend.web.core import commit_message as _cm

    try:
        return _cm._run(argv, _cm.neutral_cwd(), ONESHOT_TIMEOUT_S)
    except _cm.CommitMessageError as err:
        raise AssistantError(str(err)) from err


def _neutral_cwd() -> str:
    from backend.web.core import commit_message as _cm

    return _cm.neutral_cwd()


class _Proc:
    """One live chat process and its stdout reader."""

    def __init__(self, popen: Any, sig: tuple, settings_at: Dict[str, Any]) -> None:
        self.p = popen
        self.sig = sig
        self.settings_at = settings_at
        self.lines: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.turns = 0
        self.conversation = ""
        self.last_used = time.monotonic()
        self.started = time.monotonic()
        t = threading.Thread(
            target=self._read, name="voice-assistant-reader", daemon=True
        )
        t.start()

    def _read(self) -> None:
        try:
            for line in iter(self.p.stdout.readline, b""):
                self.lines.put(line)
        except Exception:  # noqa: BLE001 — EOF either way
            pass
        self.lines.put(None)

    def alive(self) -> bool:
        try:
            return self.p.poll() is None
        except Exception:  # noqa: BLE001
            return False

    def write(self, text: str) -> None:
        msg = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
        self.p.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.p.stdin.flush()

    def kill(self) -> None:
        for step in ("close", "kill"):
            try:
                if step == "close":
                    self.p.stdin.close()
                else:
                    self.p.kill()
            except Exception:  # noqa: BLE001
                pass


class Brain:
    """The server's one assistant process (see the module docstring)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[_Proc] = None
        self._reaper: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._history: Dict[str, List[Tuple[str, str]]] = {}

    # -- what to run --
    def _plan(self) -> Tuple[Optional[Any], tuple]:
        """``(provider, sig)``: the provider to ask and what the running
        process must match (program, model, talk level)."""
        try:
            from backend import providers

            program = assistant_program()
            prov = providers.resolve(program)
        except Exception:  # noqa: BLE001
            return None, ()
        return prov, (program, model_name(), talk_level())

    def _settings_now(self) -> List[dict]:
        try:
            return _catalog.settings()
        except Exception:  # noqa: BLE001
            return []

    def _start(self, prov: Any, sig: tuple) -> Optional[_Proc]:
        rows = self._settings_now()
        argv = prov.stream_chat_argv(build_system_prompt(sig[2], rows), sig[1])
        if not argv:
            return None
        popen = spawn_process(argv, _neutral_cwd())
        proc = _Proc(popen, sig, {r["path"]: r.get("value") for r in rows})
        self._ensure_reaper()
        return proc

    def _ensure_reaper(self) -> None:
        if self._reaper is not None and self._reaper.is_alive():
            return
        self._stop.clear()
        self._reaper = threading.Thread(
            target=self._reap_loop, name="voice-assistant-reaper", daemon=True
        )
        self._reaper.start()

    def _reap_loop(self) -> None:
        while not self._stop.wait(_REAP_EVERY_S):
            if not self.reap():
                return

    def reap(self, now: Optional[float] = None) -> bool:
        """One idle check: stop after :data:`STOP_AFTER_S`, freshen a used
        process after :data:`FRESH_AFTER_S`. False once nothing is left to
        watch."""
        now = time.monotonic() if now is None else now
        if not self._lock.acquire(blocking=False):
            return True
        try:
            proc = self._proc
            if proc is None:
                return False
            idle = now - proc.last_used
            if idle >= STOP_AFTER_S or not proc.alive():
                proc.kill()
                self._proc = None
                return False
            if proc.turns > 0 and idle >= FRESH_AFTER_S:
                proc.kill()
                self._proc = None
                prov, sig = self._plan()
                if prov is not None:
                    try:
                        self._proc = self._start(prov, sig)
                        if self._proc is not None:
                            # The fresh one's idle clock keeps the old one's,
                            # so STOP_AFTER_S still counts from the last turn.
                            self._proc.last_used = proc.last_used
                    except Exception:  # noqa: BLE001
                        self._proc = None
            return self._proc is not None
        finally:
            self._lock.release()

    def _ready(self, conversation: str, prov: Any, sig: tuple) -> Optional[_Proc]:
        """The process for this turn (lock held): restarted when dead, when
        the program/model/talk level changed, for a new conversation, or
        after :data:`FRESH_AFTER_S` idle."""
        proc = self._proc
        stale = proc is not None and (
            not proc.alive()
            or proc.sig != sig
            or (proc.turns > 0 and conversation != proc.conversation)
            or (proc.turns > 0 and time.monotonic() - proc.last_used >= FRESH_AFTER_S)
        )
        if stale:
            proc.kill()
            self._proc = proc = None
        if proc is None:
            proc = self._proc = self._start(prov, sig)
        return proc

    def warm(self) -> dict:
        """Start the process now (``POST /api/voice/assistant/warm``) →
        ``{ok, mode, model, program}``; ``mode``: stream | oneshot | none."""
        prov, sig = self._plan()
        if prov is None:
            return {"ok": False, "mode": "none", "model": model_name(), "program": ""}
        info = {"model": sig[1], "program": sig[0]}
        if not prov.stream_chat_argv("x", sig[1]):
            mode = "oneshot" if prov.oneshot_argv("x") else "none"
            return dict(info, ok=mode == "oneshot", mode=mode)
        if not self._lock.acquire(timeout=5):
            return dict(info, ok=True, mode="stream")  # a turn is running: warm
        try:
            proc = self._proc
            if proc is None or not proc.alive() or proc.sig != sig:
                if proc is not None:
                    proc.kill()
                self._proc = self._start(prov, sig)
            if self._proc is not None:
                self._proc.last_used = time.monotonic()
            return dict(info, ok=self._proc is not None, mode="stream")
        except Exception as err:  # noqa: BLE001
            return dict(info, ok=False, mode="stream", error=str(err))
        finally:
            self._lock.release()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._proc is not None:
                self._proc.kill()
                self._proc = None

    def settings_changed(self, rows: List[dict]) -> Dict[str, Any]:
        proc = self._proc
        base = proc.settings_at if proc is not None else {}
        return {
            r["path"]: r.get("value")
            for r in rows
            if base.get(r["path"], r.get("value")) != r.get("value")
        }

    # -- one turn (blocking: run in a thread) --
    def ask(
        self,
        build: Callable[[Dict[str, Any]], str],
        conversation: str,
        out: "queue.Queue",
    ) -> None:
        """Ask the model one turn; put each complete output line on ``out``
        as it completes, an ``("error", text)`` on failure, then ``None``.
        ``build(settings_changed)`` → the user message."""
        try:
            self._ask(build, conversation, out)
        except Exception as err:  # noqa: BLE001
            out.put(("error", "The assistant isn't available: %s" % _clip(err, 100)))
        finally:
            out.put(None)

    def _ask(self, build, conversation: str, out: "queue.Queue") -> None:
        prov, sig = self._plan()
        if prov is None:
            out.put(("error", "No agent CLI is set up to answer."))
            return
        if not prov.stream_chat_argv("x", sig[1]):
            self._ask_oneshot(prov, sig, build, conversation, out)
            return
        if not self._lock.acquire(timeout=TURN_TIMEOUT_S):
            out.put(("error", "The assistant is still busy with the last request."))
            return
        try:
            proc = self._ready(conversation, prov, sig)
            if proc is None:
                out.put(("error", "The assistant couldn't start."))
                return
            msg = build(self.settings_changed(self._settings_now()))
            while True:  # drop a previous turn's leftovers
                try:
                    proc.lines.get_nowait()
                except queue.Empty:
                    break
            try:
                proc.write(msg)
            except Exception:  # noqa: BLE001 — died since: one restart
                proc.kill()
                self._proc = None
                proc = self._ready(conversation, prov, sig)
                if proc is None:
                    out.put(("error", "The assistant couldn't start."))
                    return
                proc.write(msg)
            proc.turns += 1
            proc.conversation = conversation
            ok = self._read_turn(proc, out)
            proc.last_used = time.monotonic()
            if not ok:
                proc.kill()
                self._proc = None
        finally:
            self._lock.release()

    def _read_turn(self, proc: _Proc, out: "queue.Queue") -> bool:
        parser = LineParser()
        deltas = False
        deadline = time.monotonic() + TURN_TIMEOUT_S
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                out.put(("error", "The assistant took too long to answer."))
                return False
            try:
                raw = proc.lines.get(timeout=left)
            except queue.Empty:
                continue
            if raw is None:
                for line in parser.flush():
                    out.put(("line", line))
                out.put(("error", "The assistant stopped unexpectedly."))
                return False
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            kind = ev.get("type") if isinstance(ev, dict) else None
            if kind == "stream_event":
                e = ev.get("event") or {}
                d = e.get("delta") or {}
                if (
                    e.get("type") == "content_block_delta"
                    and d.get("type") == "text_delta"
                ):
                    deltas = True
                    for line in parser.feed(d.get("text") or ""):
                        out.put(("line", line))
            elif kind == "assistant" and not deltas:
                for block in (ev.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        for line in parser.feed((block.get("text") or "") + "\n"):
                            out.put(("line", line))
            elif kind == "result":
                for line in parser.flush():
                    out.put(("line", line))
                if ev.get("is_error") or (ev.get("subtype") not in (None, "success")):
                    out.put(
                        (
                            "error",
                            "The assistant's model failed: %s"
                            % _clip(ev.get("result") or ev.get("subtype"), 100),
                        )
                    )
                return True

    def _ask_oneshot(
        self, prov, sig, build, conversation: str, out: "queue.Queue"
    ) -> None:
        rows = self._settings_now()
        msg = build({})
        past = self._history.get(conversation, [])
        hist = "".join(
            "Earlier, the person said: %s\nYou answered:\n%s\n\n" % h for h in past
        )
        prompt = build_system_prompt(sig[2], rows) + "\n\n" + hist + msg
        argv = prov.oneshot_argv(prompt)
        if not argv:
            out.put(
                ("error", "%s can't answer the assistant." % (sig[0] or "This CLI"))
            )
            return
        text = run_oneshot(argv)
        for line in text.splitlines():
            if line.strip():
                out.put(("line", line))
        said = msg.rsplit("The person said: ", 1)[-1]
        self._history = {
            conversation: (past + [(said, text.strip())])[-ONESHOT_HISTORY:]
        }


#: The server's assistant.
BRAIN = Brain()
atexit.register(BRAIN.stop)


# --- one turn -------------------------------------------------------------------
async def _confirmed(
    confirm: dict, ctx: Context, conversation: str
) -> AsyncIterator[dict]:
    action = _take(str(confirm.get("id") or ""))
    if action is None:
        yield {"type": "error", "text": "That question has expired; please ask again."}
        return
    if confirm.get("yes") is not True:
        return
    async for ev in _execute(action, ctx, conversation):
        yield ev


async def _model_lines(
    brain: Brain, build, conversation: str
) -> AsyncIterator[Tuple[str, str]]:
    out: "queue.Queue" = queue.Queue()
    worker = threading.Thread(
        target=brain.ask,
        args=(build, conversation, out),
        name="voice-assistant-turn",
        daemon=True,
    )
    worker.start()
    while True:
        item = await asyncio.to_thread(out.get)
        if item is None:
            return
        yield item


async def turn(
    payload: dict, ctx: Context, brain: Optional[Brain] = None
) -> AsyncIterator[dict]:
    """``POST /api/voice/assistant``'s events for ``payload`` (``text``,
    ``conversation``, ``snapshot``, ``confirm``?), ``done`` last."""
    t0 = time.monotonic()
    brain = brain or BRAIN
    text = " ".join(str(payload.get("text") or "").split())[:TEXT_MAX]
    conversation = str(payload.get("conversation") or "")
    snapshot = (
        payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
    )
    confirm = payload.get("confirm")

    def done(model: bool) -> dict:
        return {
            "type": "done",
            "ms": int((time.monotonic() - t0) * 1000),
            "model": model,
        }

    if isinstance(confirm, dict) and confirm.get("id"):
        async for ev in _confirmed(confirm, ctx, conversation):
            yield ev
        yield done(False)
        return
    if not text:
        yield {"type": "error", "text": "I didn't catch that."}
        yield done(False)
        return
    quick = instant(text, snapshot)
    if quick is not None:
        yield {"type": "act", "action": quick}
        yield done(False)
        return

    rows = await ctx.rows()
    try:
        repos = await ctx.repos()
    except Exception:  # noqa: BLE001 — the names are a hint
        repos = []
    talk = talk_level()

    def build(changed: Dict[str, Any]) -> str:
        return build_turn_message(text, snapshot, rows, repos, changed)

    acted = False
    emitted = False
    prose: List[str] = []
    async for kind, value in _model_lines(brain, build, conversation):
        if kind == "error":
            emitted = True
            yield {"type": "error", "text": value}
            continue
        item = parse_line(value)
        if item is None:
            continue
        k = item["kind"]
        if k == "prose":
            prose.append(item["text"])
            continue
        if k == "bad":
            emitted = True
            yield {"type": "error", "text": "Sorry, I couldn't work out what to do."}
            continue
        if k in ("say", "ask"):
            said = _spoken(item["text"])
            if not said:
                continue
            if k == "say" and acted and talk != "chatty" and _echo(said):
                continue  # no spoken echo of what was just done
            emitted = True
            yield {"type": k, "text": said}
            continue
        action, problem, destructive = validate(item["action"], snapshot, rows)
        emitted = True
        if problem is not None:
            yield problem
            continue
        if k == "confirm" or destructive:
            cid = _hold(action, conversation)
            question = _spoken(item.get("text") or "") if k == "confirm" else ""
            yield {
                "type": "confirm",
                "id": cid,
                "text": question or _confirm_text(action),
                "action": action,
            }
            continue
        acted = True
        async for ev in _execute(action, ctx, conversation):
            yield ev
    if not emitted:
        said = _spoken(" ".join(prose))
        if said:
            yield {"type": "say", "text": said}
        else:
            yield {"type": "error", "text": "Sorry, I didn't get an answer."}
    yield done(True)
