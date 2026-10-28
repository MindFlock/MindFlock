"""A session is asking you something: the first-class ``asking`` signal.

Two ways an agent waits on a person, one shape for both (the instance rows'
``asking`` field, ``GET /dialog``, the outbox, the voice router and the
``needs_input`` push all read it):

* ``kind: "dialog"`` — the agent is blocked on a dialog: its activity reads
  ``clarify`` (a parsed menu on screen, the CLI's own hook, or a stable
  waiting-prompt pattern). ``text`` / ``dialog_id`` / ``parsed`` come from the
  screen the activity probe ALREADY captured (``agent_state.last_screen``),
  parsed at most once per distinct screen; with no fresh capture they are
  null and the clients fetch ``/dialog``. Never a tmux call from here.
* ``kind: "question"`` — the agent asked in plain prose and its turn ended:
  the session is idle, its newest reply ENDS with a question
  (:func:`question_in`), and nobody has put input into it since. Nothing on
  screen blocks; the answer is a prompt (``/send``). **Only while
  ``notifications.prose_questions`` is on** (:func:`prose_on`; off by
  default): the owner wants a Question only for a real prompt — a
  permission, a plan approval, Claude's question menu — not for a reply
  that happens to end with "Should I also…?". Off, :func:`current`,
  :func:`announce` and :func:`peek` report nothing for this kind, so the
  row, the bell, the outbox, the push and the voice router stay quiet.

The reply text comes from, in order: the Stop hook's reply file
(``spoken.read_reply``, Claude), the provider's newest assistant text
(``last_assistant_text`` — Claude's transcript, Codex's rollout), else the
last text paragraph above the CLI's input prompt on the probe's cached screen
(:func:`screen_reply`, every other CLI). All three are cache hits on the poll
path.

State per title (:data:`_STATE`): the reply this module last looked at (its
identity ``key``, its ``ts`` — the reply file's stamp, else when it was first
seen — and the question in it), the reply the person dismissed, and the reply
whose ``session.question`` was announced. A question is suppressed once any
input reaches the session after it (:func:`note_input`: every human input
path through ``state_events._note_human_input``, plus a queued prompt going
in — the ``session.prompt_sent`` subscriber below), or once dismissed
(:func:`dismiss`); a NEWER reply asks again. The announcement memory is seeded
on first sighting (:func:`seed`) like ``state_events._REPLY_SEEN``, so a
restart does not re-announce a question a dead server already pushed.

**What survives a restart** (``mindflock restart`` re-execs the server in
place; an update does too): the dismissals and the answered-marks, in
``~/.mindflock/asking.json`` (:func:`_store_path`: under ``MINDFLOCK_HOME``
when set; none under the pytest home guard, so tests stay in memory).
Without it a question put off with Later, or answered in a way that left no
newer reply, came back as a Question chip after every restart — a reply that
ended with a question at 5 PM was asking again at 8 PM. The file is written
only when a dismissal or an answer changes what shows (``note_input`` fires
on every keystroke; it writes only while that session has a question), and
holds at most :data:`STORE_MAX` sessions.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from backend.web.core import spoken as _spoken

__all__ = [
    "QUESTION_MAX_CHARS",
    "announce",
    "current",
    "dialog_asking",
    "dismiss",
    "forget",
    "note_input",
    "peek",
    "question_id",
    "question_in",
    "screen_reply",
    "seed",
]

#: Ceiling on ``asking.text`` (characters), both kinds.
QUESTION_MAX_CHARS = 300
#: How many sessions' dismissals / answered-marks the store keeps (the
#: oldest answered-marks go first).
STORE_MAX = 200
#: A cached screen older than this is not used (seconds): the probe captures
#: every ~4 s while anyone polls, so anything older describes another moment.
SCREEN_MAX_AGE_S = 15.0

# --- finding the question ------------------------------------------------------ #
_FENCE_LINE_RE = re.compile(r"^\s*(?:```|~~~)")
_INLINE_CODE_RE = re.compile(r"`+([^`]*)`+")
_LIST_LINE_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)]|[a-zA-Z][.)])\s+\S")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])[\"'”’)\]]*\s+")
_ENDS_ASKING_RE = re.compile(r"\?[\"'”’)\]*_]*$")
#: A ``?`` mentioned as a character — quoted on its own (``the "?" key``).
_QUOTED_QMARK_RE = re.compile(r"([\"'“‘])\?([\"'”’])")
#: Stands in for a ``?`` inside inline code (or quoted on its own) while
#: sentences are judged, so ``foo?`` in backticks never makes a sentence a
#: question.
_CODE_QMARK = "\x00"


def _cap(text: str, limit: int = QUESTION_MAX_CHARS) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    sp = cut.rfind(" ")
    if sp > limit // 2:
        cut = cut[:sp]
    return cut.rstrip(" ,;:—–-") + "…"


def _paragraphs(text: str) -> list:
    """``text``'s paragraphs (blank-line separated), fenced code dropped (an
    unclosed fence swallows the rest, as a renderer would show it)."""
    paras, cur, in_fence = [], [], False
    for raw in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _FENCE_LINE_RE.match(raw):
            in_fence = not in_fence
            if cur:
                paras.append(cur)
                cur = []
            continue
        if in_fence:
            continue
        if not raw.strip():
            if cur:
                paras.append(cur)
                cur = []
            continue
        cur.append(raw)
    if cur:
        paras.append(cur)
    return paras


def _is_say_line(line: str) -> bool:
    return bool(_spoken._SAY_LINE_RE.match(line.strip()))


def _asking_sentences(lines: list) -> Optional[str]:
    """The question sentences of one paragraph, or None when it asks nothing:
    the sentences that end in ``?`` (outside inline code); failing those, the
    paragraph itself when it opens with a question word ("Should I …", "Do
    you …" — :data:`spoken._QUESTION_START_RE`)."""
    joined = "\n".join(lines)
    masked = _INLINE_CODE_RE.sub(lambda m: m.group(0).replace("?", _CODE_QMARK), joined)
    masked = _QUOTED_QMARK_RE.sub(
        lambda m: m.group(1) + _CODE_QMARK + m.group(2), masked
    )
    plain = _spoken.strip_markdown(masked)
    if not plain:
        return None
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(plain) if s.strip()]
    asked = [s for s in sentences if _ENDS_ASKING_RE.search(s)]
    if asked:
        return _cap(" ".join(asked).replace(_CODE_QMARK, "?"))
    if _spoken._QUESTION_START_RE.match(plain):
        return _cap(plain.replace(_CODE_QMARK, "?"))
    return None


def question_in(text) -> Optional[str]:
    """The question a reply ENDS with, or None.

    * A ``Say:`` line decides when the reply has one — it is the model's own
      line for the listener, and the voice instruction asks it to carry any
      question: the line's question sentences when it asks, else None.
    * Otherwise the reply's FINAL paragraph (fenced code dropped, markdown
      stripped) must ask — a sentence ending in ``?`` (a ``?`` inside inline
      code or quoted on its own, ``the "?" key``, does not count), or an
      opening question word. One exception: a
      final paragraph that is only a list (the options) defers to the
      paragraph just above it, which must then END in ``?`` ("Which do you
      want?" then "1. A / 2. B").
    * A ``?`` in an earlier paragraph, or inside a code block, is not a
      question: the agent moved on after it.

    Conservative by design: "Let me know which you prefer." is not a question
    (no ``?``, no question word); "I fixed it. Done?" is ("Done?"). Returns
    the question sentence(s), whitespace collapsed, ≤ :data:`QUESTION_MAX_CHARS`.
    """
    if not text or not str(text).strip():
        return None
    say = _spoken.extract_say(str(text))
    if say:
        return _asking_sentences([say])
    paras = [[ln for ln in p if not _is_say_line(ln)] for p in _paragraphs(str(text))]
    paras = [p for p in paras if p]
    if not paras:
        return None
    last = paras[-1]
    got = _asking_sentences(last)
    if got:
        return got
    if len(paras) >= 2 and all(_LIST_LINE_RE.match(ln) for ln in last):
        above = _asking_sentences(paras[-2])
        if above and above.endswith("?"):
            return above
    return None


# --- the screen fallback ------------------------------------------------------- #
#: An input-prompt line: the CLI's prompt glyph (inside a box or not), with
#: at most a short draft after it.
_PROMPT_LINE_RE = re.compile(r"^[\s│┃|]*(?:>|❯|›|»|➜)(?:\s.{0,80})?\s*[│┃|]?\s*$")
_BORDER_RE = re.compile(r"^[\s─━═╭╮╰╯┌┐└┘├┤┬┴┼│┃|+\-_=~.·•]*$")
_BULLET_RE = re.compile(r"^[\s│┃]*(?:⏺|●|◆|▌|▍|•|\*)\s+")
#: How many lines up from the bottom the input prompt is looked for (under
#: it: a status line, a hint row, a box border).
_PROMPT_SEARCH_LINES = 10


def screen_reply(screen) -> Optional[str]:
    """The final text paragraph above the CLI's input prompt on ``screen``
    (the pane's visible capture), or None when no input prompt is near the
    bottom (best-effort, for a CLI with no reply file and no transcript).
    Box borders and the reply's bullet glyph are taken off."""
    if not screen:
        return None
    lines = [ln.rstrip() for ln in str(screen).replace("\r", "").split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    prompt_at = None
    for i in range(len(lines) - 1, max(-1, len(lines) - 1 - _PROMPT_SEARCH_LINES), -1):
        if _PROMPT_LINE_RE.match(lines[i]):
            prompt_at = i
            break
    if prompt_at is None:
        return None
    i = prompt_at - 1
    while i >= 0 and (not lines[i].strip() or _BORDER_RE.match(lines[i])):
        i -= 1
    para = []
    while i >= 0 and lines[i].strip() and not _BORDER_RE.match(lines[i]):
        para.append(_BULLET_RE.sub("", lines[i].strip(" │┃|")).strip())
        i -= 1
    para.reverse()
    text = "\n".join(p for p in para if p)
    return text or None


# --- state ---------------------------------------------------------------------- #
_LOCK = threading.Lock()
#: title -> {"key", "ts", "text", "source", "dismissed", "announced", "seeded"}
_STATE: Dict[str, dict] = {}
#: title -> epoch of the last input into the session (note_input).
_INPUT_AT: Dict[str, float] = {}
#: title -> (screen hash, body) — one dialog parse per distinct screen.
_DIALOG_MEMO: Dict[str, tuple] = {}
#: title -> the dismissed reply key, as loaded from the store (JSON lists);
#: applied to a title's record the first time it is observed.
_DISMISSED_STORED: Dict[str, list] = {}
_STORE_LOADED = False


def _store_path() -> Optional[Path]:
    """``~/.mindflock/asking.json`` — under ``MINDFLOCK_HOME`` when set; None
    with no home to use (the pytest guard refused the real one)."""
    home = os.environ.get("MINDFLOCK_HOME", "").strip()
    if home:
        return Path(os.path.expanduser(home)) / "asking.json"
    try:
        from backend.config.config import GetConfigDir

        return Path(GetConfigDir()) / "asking.json"
    except Exception:  # noqa: BLE001 — the home guard refused the real home
        return None


def _load_store() -> None:
    """Read the store once per process (under ``_LOCK``)."""
    global _STORE_LOADED
    if _STORE_LOADED:
        return
    _STORE_LOADED = True
    path = _store_path()
    if path is None:
        return
    try:
        from backend.fsutil import read_json

        data = read_json(path, default=None)
    except Exception:  # noqa: BLE001 — an unreadable store is an empty one
        data = None
    if not isinstance(data, dict):
        return
    raw = data.get("dismissed")
    if isinstance(raw, dict):
        for title, key in raw.items():
            if isinstance(title, str) and isinstance(key, list) and len(key) == 2:
                _DISMISSED_STORED[title] = key
    raw = data.get("input_at")
    if isinstance(raw, dict):
        for title, at in raw.items():
            if isinstance(title, str) and isinstance(at, (int, float)):
                _INPUT_AT.setdefault(title, float(at))


def _save_store() -> None:
    """Write what suppresses a question (called under ``_LOCK``). Best
    effort: a failed write costs one repeated chip after a restart."""
    path = _store_path()
    if path is None:
        return
    dismissed: Dict[str, list] = dict(_DISMISSED_STORED)
    for title, st in _STATE.items():
        key = st.get("dismissed")
        if key:
            dismissed[title] = list(key)
    input_at = {t: at for t, at in _INPUT_AT.items() if at}
    if len(input_at) > STORE_MAX:
        keep = sorted(input_at, key=input_at.get)[-STORE_MAX:]
        input_at = {t: input_at[t] for t in keep}
    if len(dismissed) > STORE_MAX:
        dismissed = dict(list(dismissed.items())[-STORE_MAX:])
    try:
        from backend.fsutil import atomic_write_json

        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            path,
            {"version": 1, "dismissed": dismissed, "input_at": input_at},
            ensure_ascii=True,
            mode=0o600,
        )
    except Exception:  # noqa: BLE001
        pass


def prose_on() -> bool:
    """``notifications.prose_questions`` (default off; an unreadable store
    says off)."""
    try:
        from backend.config import settings as settings_store

        return bool(settings_store.load_settings().notifications.prose_questions)
    except Exception:  # noqa: BLE001
        return False


def question_id(ts) -> str:
    """The ``/dialog`` id of a prose question (``"q:<ts>"``)."""
    return "q:%.3f" % float(ts or 0.0)


def note_input(title: str) -> None:
    """Input reached ``title`` (a person, or a queued prompt): any question
    its newest reply asked is answered or superseded. Remembered on disk
    while the session has a question, so a restart does not ask it again."""
    if not title:
        return
    _INPUT_AT[title] = time.time()
    with _LOCK:
        _load_store()
        if _STATE.get(title, {}).get("text"):
            _save_store()


def forget(title: str) -> None:
    """Drop ``title``'s state (session removal)."""
    with _LOCK:
        _load_store()
        had = title in _STATE or title in _DISMISSED_STORED or title in _INPUT_AT
        _STATE.pop(title, None)
        _DIALOG_MEMO.pop(title, None)
        _DISMISSED_STORED.pop(title, None)
        _INPUT_AT.pop(title, None)
        if had:
            _save_store()


def _session_name(title: str) -> str:
    from backend.session import tmux

    return tmux.to_mindflock_tmux_name(title or "")


def _provider(inst):
    from backend import providers

    return providers.resolve(getattr(inst, "Program", "") or "")


def _reply(title: str, inst) -> Optional[tuple]:
    """``(key, ts, text, source)`` of ``title``'s newest reply, or None. ``ts``
    is None when the source has no stamp (the caller then uses first-seen)."""
    name = _session_name(title)
    got = _spoken.read_reply(name)
    if got and got[1] and got[1].strip():
        ts, text = got
        return ("hook", float(ts or 0.0)), float(ts or 0.0) or None, text, "hook"
    if inst is not None:
        text = _spoken._last_assistant_text(inst, name)
        if isinstance(text, str) and text.strip():
            digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
            return ("transcript", digest), None, text, "transcript"
    try:
        from backend.web.core import agent_state as _agent_state

        screen = _agent_state.last_screen(title, SCREEN_MAX_AGE_S)
    except Exception:  # noqa: BLE001 — no screen reads as no reply
        screen = None
    text = screen_reply(screen) if screen else None
    if text:
        digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
        return ("screen", digest), None, text, "screen"
    return None


def _observe(title: str, inst) -> Optional[dict]:
    """Refresh ``title``'s reply record from its sources → the record (a
    copy), or None when there is no reply at all."""
    got = _reply(title, inst)
    if got is None:
        return None
    key, ts, text, source = got
    with _LOCK:
        _load_store()
        st = _STATE.setdefault(title, {})
        if st.get("key") != key:
            st.update(
                key=key,
                ts=ts if ts else time.time(),
                text=question_in(text),
                source=source,
            )
            # Put off before a restart: still put off, for that reply only.
            stored = _DISMISSED_STORED.pop(title, None)
            if stored is not None and tuple(stored) == tuple(key):
                st["dismissed"] = key
        return dict(st)


def seed(title: str, inst) -> None:
    """First sighting of ``title`` (state_events' first pass): whatever its
    newest reply asks is old news to the announcer — never pushed again by
    this process. Never raises."""
    try:
        with _LOCK:
            if _STATE.get(title, {}).get("seeded"):
                return
        rec = _observe(title, inst)
        with _LOCK:
            st = _STATE.setdefault(title, {})
            st["seeded"] = True
            if rec is not None:
                st["announced"] = rec.get("key")
    except Exception:  # noqa: BLE001 — provenance is best-effort
        pass


def _armed(rec: Optional[dict], title: str) -> bool:
    if not rec or not rec.get("text"):
        return False
    if rec.get("dismissed") is not None and tuple(rec.get("dismissed")) == tuple(
        rec.get("key") or ()
    ):
        return False
    return _INPUT_AT.get(title, 0.0) <= float(rec.get("ts") or 0.0)


def _question(rec: dict) -> dict:
    return {
        "kind": "question",
        "text": rec["text"],
        "ts": float(rec.get("ts") or 0.0),
        "dialog_id": None,
        "parsed": False,
        "source": rec.get("source"),
    }


def dialog_asking(title: str, inst) -> dict:
    """``asking`` for a session whose activity is ``clarify``: the dialog off
    the probe's cached screen (parsed once per distinct screen), or the
    null-text shape when there is no fresh capture."""
    from backend.web.core import agent_state as _agent_state

    try:
        since = float(_agent_state.state_since(title) or 0.0)
    except Exception:  # noqa: BLE001
        since = 0.0
    out = {
        "kind": "dialog",
        "text": None,
        "ts": since or time.time(),
        "dialog_id": None,
        "parsed": False,
        "source": None,
    }
    try:
        screen = _agent_state.last_screen(title, SCREEN_MAX_AGE_S)
    except Exception:  # noqa: BLE001
        screen = None
    if not screen:
        return out
    h = hashlib.sha1(screen.encode("utf-8", "replace")).hexdigest()
    with _LOCK:
        hit = _DIALOG_MEMO.get(title)
    if hit is not None and hit[0] == h:
        body = hit[1]
    else:
        from backend.providers import dialogs

        try:
            parsed = _provider(inst).parse_dialog(screen)
        except Exception:  # noqa: BLE001 — a parser bug reads as "unparsed"
            parsed = None
        try:
            body = dialogs.describe(
                parsed if isinstance(parsed, dict) else None, screen
            )
        except Exception:  # noqa: BLE001
            body = None
        with _LOCK:
            _DIALOG_MEMO[title] = (h, body)
    if not isinstance(body, dict):
        return out
    q = body.get("question")
    out.update(
        text=_cap(q) if q else None,
        dialog_id=body.get("id"),
        parsed=bool(body.get("parsed")),
        source=body.get("source"),
    )
    return out


def current(title: str, inst, activity: str) -> Optional[dict]:
    """``title``'s ``asking`` row field for its current ``activity``: the
    dialog shape on ``clarify``, the question shape on ``idle`` while its
    newest reply asks and nothing has answered it, else None. Cheap (cached
    reads only) and never raises."""
    try:
        if activity == "clarify":
            return dialog_asking(title, inst)
        if activity != "idle" or not prose_on():
            return None
        rec = _observe(title, inst)
        return _question(rec) if _armed(rec, title) else None
    except Exception:  # noqa: BLE001 — enrichment, never a poll failure
        return None


def dismiss(title: str) -> bool:
    """Suppress the question ``title``'s newest reply asks (a newer reply asks
    again). → whether there was one."""
    with _LOCK:
        _load_store()
        st = _STATE.get(title)
        if not st or not st.get("key"):
            return False
        had = bool(st.get("text")) and st.get("dismissed") != st.get("key")
        st["dismissed"] = st.get("key")
        if had:
            _save_store()
        return had


def announce(title: str, inst) -> Optional[dict]:
    """The ``session.question`` data for ``title``'s turn end, exactly once
    per distinct reply: ``{title, text, ts, kind: "question"}`` while its
    newest reply asks (and is not answered or dismissed) and has not been
    announced, else None. The caller emits; claiming here is what makes it
    once. Never raises."""
    try:
        if not prose_on():
            return None
        rec = _observe(title, inst)
        if not _armed(rec, title):
            return None
        with _LOCK:
            st = _STATE.setdefault(title, {})
            if not st.get("seeded"):
                # Never sighted before this turn end: provenance unknown.
                st["seeded"] = True
                st["announced"] = rec.get("key")
                return None
            if st.get("announced") == rec.get("key"):
                return None
            st["announced"] = rec.get("key")
        return {
            "title": title,
            "text": rec["text"],
            "ts": float(rec.get("ts") or 0.0),
            "kind": "question",
        }
    except Exception:  # noqa: BLE001
        return None


def peek(title: str) -> Optional[str]:
    """The question ``title`` is asking right now per the last observation
    (no source read), or None."""
    if not prose_on():
        return None
    with _LOCK:
        rec = dict(_STATE.get(title) or {})
    return rec.get("text") if _armed(rec, title) else None


def _on_event(envelope: dict) -> None:
    """A queued prompt went in: that is input, like a person's."""
    if envelope.get("event") == "session.prompt_sent":
        note_input(str(envelope.get("session") or ""))


def _subscribe() -> None:
    try:
        from backend.web.core import events as _events

        _events.BUS.subscribe(_on_event)
    except Exception:  # noqa: BLE001 — without it a queue send just reads later
        pass


_subscribe()
