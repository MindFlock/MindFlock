"""The voice router: one finished phrase → a command, or a prompt.

``POST /api/voice/route`` (routes/voice.py) hands every phrase the person
says to :func:`answer`; the phone page (``/m``) and the desktop mic both call
it, so the grammar lives here once. The phone keeps its own table
(mobile.js ``VOICE_CMD``) only as an offline fallback, and the rules below are
a port of it: the same normalisation (:func:`norm`), the same session
matcher (prefix → word overlap → loose containment, :func:`match_session`),
the same attention rule as the rail's bell (:func:`session_state`) and the
same sentences.

Pure: functions take the ``/api/instances`` rows (``title``, ``name``,
``activity``, ``activity_since``, ``last_say``, ``last_turn``, …), the known
repositories and the text, and return the answer dict. No I/O — the route
gathers the rows, resolves the title against the registry and creates the
session.

The default is ``{"kind": "prompt"}``: anything that is not plainly a command
is words for the current session. A false command is worse than a missed one
(the missed one still reaches the agent), so every pattern matches the WHOLE
phrase.
"""

from __future__ import annotations

import re
import time
from typing import Iterable, List, Optional, Tuple

from backend.web.core.session_create import title_from_prompt
from backend.web.core.spoken import strip_markdown

__all__ = [
    "LOCAL_KINDS",
    "answer",
    "command",
    "match_repo",
    "match_session",
    "norm",
    "plan_new",
    "session_state",
    "started",
    "title_base",
]

#: Commands the client carries out itself: the answer has no ``say``.
LOCAL_KINDS = ("cancel", "stop", "off", "repeat", "condense")
#: At most this many names in a "Which one" / "No session called" sentence.
MAX_NAMES = 6
#: At most this many sessions read out by "what needs me".
MAX_NEEDS = 3
#: At most this many sessions read out by "list sessions".
MAX_LIST = 10
#: Idle this long on uncommitted work and the bell lists it as stuck
#: (mobile.js ``STUCK_IDLE_S``).
STUCK_IDLE_S = 20 * 60
#: Longest title made from a "called <name>" (the New dialog's cap).
TITLE_MAX = 32
#: Words of the task a title is made from.
TITLE_WORDS = 4
#: The title when there is neither a name nor a task.
DEFAULT_TITLE = "voice-session"

# The phone's table, in its order (mobile.js VOICE_CMD), plus "list".
_CMDS = [
    ("cancel", re.compile(r"^(?:cancel|never ?mind|scratch that|forget it)$")),
    ("switch", re.compile(r"^(?:switch|go|change|jump) (?:over )?to (.+)$")),
    (
        "list",
        re.compile(
            r"^(?:(?:what|which) sessions (?:do i have|are (?:up|open|running))"
            r"|list (?:the |my )?sessions"
            r"|what(?:'?s| is) (?:up|open|running))$"
        ),
    ),
    ("needs", re.compile(r"^(?:what|who) needs (?:me|attention|my attention)$")),
    (
        "status",
        re.compile(
            r"^(?:what'?s it doing|what is it doing|status|what'?s happening"
            r"|what'?s going on)$"
        ),
    ),
    (
        "condense",
        re.compile(r"^(?:summari[sz]e(?: that| it)?|what did it say|summary)$"),
    ),
    ("repeat", re.compile(r"^(?:say that again|repeat(?: that)?)$")),
    ("off", re.compile(r"^(?:voice off|turn voice off|turn off voice)$")),
    ("stop", re.compile(r"^(?:stop listening|stop)$")),
]

# "(start|open|create|make|spin up)? (a|another)? new session (called|named
# <name>)? (in|for|on <repo>)? (to|and|that <task>)?" — on the normalised
# text, where ":", "—" and "," are already spaces (so "new session: fix it"
# is the bare form below).
_NEW_RE = re.compile(
    r"^(?:(?:start|open|create|make|spin up) )?(?:(?:a|another) )?new session"
    r"(?: (?:called|named) (?P<name>.+?))?"
    r"(?: (?P<prep>in|for|on) (?P<repo>.+?))?"
    r"(?: (?:to|and|that) (?P<task>.+))?$"
)
# The bare "new session <task>".
_NEW_BARE_RE = re.compile(r"^new session (?P<task>.+)$")
_LEAD = ("ok", "okay", "hey", "please")
# Words around a spoken repository name that are not part of it.
_REPO_LEAD = ("the", "my", "our")
_REPO_TAIL = ("repo", "repository", "project", "folder", "codebase", "checkout")


def norm(text) -> str:
    """mobile.js ``voiceNorm``: lowercase, curly quotes straightened, anything
    but letters, digits, ``'`` and spaces made a space, spaces collapsed."""
    s = str(text or "").lower().replace("’", "'").replace("`", "'")
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _end(text) -> str:
    """mobile.js ``endSentence``: a full stop unless it ends in one."""
    s = str(text or "").strip()
    return s if not s or s[-1] in ".!?" else s + "."


# --- words ↔ the original text ---------------------------------------------
class _Words:
    """The phrase as normalised words, each remembering the whitespace token
    of the ORIGINAL text it came from, so a task is handed on as it was said
    ("fix auth.py", not "fix auth py"). The phone's lead word (ok/okay/hey/
    please) and trailing "please" are dropped, as in ``voiceCommand``."""

    def __init__(self, text) -> None:
        self.toks = str(text or "").split()
        words: List[str] = []
        owner: List[int] = []
        for i, tok in enumerate(self.toks):
            for w in norm(tok).split():
                words.append(w)
                owner.append(i)
        if len(words) > 1 and words[0] in _LEAD:
            words, owner = words[1:], owner[1:]
        if len(words) > 1 and words[-1] == "please":
            words, owner = words[:-1], owner[:-1]
        self.words = words
        self.owner = owner
        self.text = " ".join(words)

    def at(self, offset: int) -> int:
        """The word index of character ``offset`` in :attr:`text`."""
        return self.text[:offset].count(" ")

    def original(self, start: int, end: Optional[int] = None) -> str:
        """Words ``start``..``end`` as the person said them (whole tokens),
        stray leading punctuation (":", "—") and a trailing comma dropped."""
        end = len(self.words) if end is None else end
        if start >= end:
            return ""
        out = " ".join(self.toks[self.owner[start] : self.owner[end - 1] + 1])
        return out.strip().lstrip(":;,—–-").strip().rstrip(",;:").strip()


def _parse_new(w: _Words) -> Optional[dict]:
    """The "new session" phrase → word spans, or None.

    ``{"name": (a, b) | None, "prep": str, "repo": (a, b) | None, "task": a |
    None}`` — word indexes into ``w.words``; the task runs to the end."""
    m = _NEW_RE.match(w.text) or _NEW_BARE_RE.match(w.text)
    if m is None:
        return None

    def span(group: str):
        if group not in m.re.groupindex or m.group(group) is None:
            return None
        return (w.at(m.start(group)), w.at(m.end(group)) + 1)

    task = span("task")
    return {
        "name": span("name"),
        "prep": m.groupdict().get("prep") or "",
        "repo": span("repo"),
        "task": task[0] if task else None,
    }


def command(text) -> Optional[dict]:
    """The command ``text`` is, or None (a prompt): ``{"kind", "arg"}``, and
    for ``new`` ``{"kind": "new", "name", "repo", "task"}`` as said (the repo
    words unresolved — :func:`plan_new` resolves them)."""
    w = _Words(text)
    if not w.text:
        return None
    for kind, rx in _CMDS[:1]:
        if rx.match(w.text):
            return {"kind": kind, "arg": ""}
    p = _parse_new(w)
    if p is not None:
        return {
            "kind": "new",
            "name": " ".join(w.words[p["name"][0] : p["name"][1]]) if p["name"] else "",
            "repo": " ".join(w.words[p["repo"][0] : p["repo"][1]]) if p["repo"] else "",
            "task": w.original(p["task"]) if p["task"] is not None else "",
        }
    for kind, rx in _CMDS[1:]:
        m = rx.match(w.text)
        if m:
            return {
                "kind": kind,
                "arg": (m.group(1) or "").strip() if m.groups() else "",
            }
    return None


# --- sessions ----------------------------------------------------------------
def _name(row: dict) -> str:
    """What a session is called (mobile.js ``nameOf``): its ``name``, else
    its title."""
    return str(row.get("name") or row.get("title") or "")


def _listed(rows: Iterable) -> List[dict]:
    """The rows a person can talk about: titled, and not hidden off the rail
    by the feature that owns them (a Verify checklist's session)."""
    return [
        r
        for r in rows or ()
        if isinstance(r, dict) and r.get("title") and not r.get("hidden_by")
    ]


def match_session(query, rows) -> Tuple[Optional[dict], List[dict]]:
    """mobile.js ``voiceMatchSession`` → ``(hit, candidates)``.

    Names and titles, normalised: a unique prefix match, else the unique best
    word overlap, else a unique loose containment; a tie lists the tied
    rows, no match an empty list."""
    q = norm(query)
    cands = [
        {"row": r, "n": norm(_name(r)), "t": norm(r.get("title"))}
        for r in _listed(rows)
    ]
    if not q:
        return None, [c["row"] for c in cands]
    pre = [c for c in cands if c["n"].startswith(q) or c["t"].startswith(q)]
    if len(pre) == 1:
        return pre[0]["row"], []
    if pre:
        return None, [c["row"] for c in pre]
    qw = q.split(" ")
    scored = []
    for c in cands:
        words = (c["n"] + " " + c["t"]).split(" ")
        n = sum(1 for x in qw if x in words)
        if n > 0:
            scored.append((n, c))
    scored.sort(key=lambda x: -x[0])  # stable, like Array.prototype.sort
    if len(scored) == 1 or (len(scored) > 1 and scored[0][0] > scored[1][0]):
        return scored[0][1]["row"], []
    if scored:
        return None, [c["row"] for _, c in scored]
    loose = [c for c in cands if q in c["n"] or q in c["t"] or (c["n"] and c["n"] in q)]
    if len(loose) == 1:
        return loose[0]["row"], []
    return None, [c["row"] for c in loose]


def _row(rows, title) -> Optional[dict]:
    if not title:
        return None
    return next((r for r in _listed(rows) if r.get("title") == title), None)


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def session_state(row: dict, now: Optional[float] = None) -> dict:
    """mobile.js ``sessionState`` → ``{"state", "label", "reason", "in_bell"}``.

    ``in_bell`` is the rail bell's attention set — a question, a failed
    setup or pre-commit, failing checks, a gone terminal, or idle with
    unfinished work for :data:`STUCK_IDLE_S` — the one rule the phone's "what
    needs me" reads too."""
    now = time.time() if now is None else now

    def st(state, label, reason="", in_bell=False):
        return {"state": state, "label": label, "reason": reason, "in_bell": in_bell}

    status = str(row.get("status") or "")
    setup = row.get("setup") if isinstance(row.get("setup"), dict) else {}
    if row.get("workspace_missing"):
        return st("offline", "Missing")
    if status == "paused":
        return st("idle", "Paused")
    if row.get("pending") or status == "loading":
        return st("starting", "Starting")
    if row.get("sleeping"):
        return st("idle", "Sleeping")
    if setup.get("state") == "running":
        return st("starting", "Starting")
    if setup.get("state") == "failed":
        return st("needs_you", "Setup failed", "setup_failed", True)
    act = row.get("activity") or "idle"
    if act == "clarify":
        return st("needs_you", "Question", "question", True)
    if act == "working":
        return st("working", "Working")
    if act == "limit":
        q = row.get("queue") if isinstance(row.get("queue"), dict) else {}
        until = _num(q.get("limited_until"))
        label = "Waiting on limit"
        if until > now:
            label += ", resumes " + time.strftime("%H:%M", time.localtime(until))
        return st("limit", label)
    if act == "offline":
        return st("offline", "Offline", "", True)
    if row.get("stage") == "interrupt":
        return st("needs_you", "Pre-commit failed", "precommit_failed", True)
    check = row.get("check") if isinstance(row.get("check"), dict) else {}
    if check.get("state") == "failed" and not check.get("stale"):
        return st("needs_you", "Checks failing", "checks_failing", True)
    since = _num(row.get("activity_since"))
    idle_for = now - since if since > 0 else 0
    ds = row.get("diff_stat") if isinstance(row.get("diff_stat"), dict) else {}
    un = ds.get("uncommitted") if isinstance(ds.get("uncommitted"), dict) else {}
    dirty = _num(un.get("additions")) + _num(un.get("deletions")) > 0
    if idle_for > STUCK_IDLE_S and dirty:
        return st("idle", "Idle", "stuck", True)
    return st("idle", "Idle")


def _attn_rank(row: dict, now: float) -> int:
    """mobile.js ``attnRank`` for a bell row: a question first, then whatever
    is broken, then failing checks."""
    reason = session_state(row, now)["reason"]
    return 0 if reason == "question" else 2 if reason == "checks_failing" else 1


def _state_word(ss: dict) -> str:
    """mobile.js ``voiceStateWord``: the state as a voice says it."""
    return {
        "question": "waiting on a question",
        "precommit_failed": "stuck on a failed pre-commit",
        "checks_failing": "waiting on failing checks",
        "setup_failed": "stuck on a failed setup",
    }.get(ss["reason"], ss["label"].lower())


def _names(rows: List[dict], n: int = MAX_NAMES) -> str:
    return ", ".join(_name(r) for r in rows[:n])


def _switch(arg: str, rows) -> dict:
    hit, cands = match_session(arg, rows)
    if hit is None:
        if cands:
            return {"kind": "ambiguous", "say": "Which one: %s?" % _names(cands)}
        everyone = _listed(rows)
        if not everyone:
            return {"kind": "no-session", "say": "There are no sessions."}
        return {
            "kind": "no-session",
            "say": "No session called %s. Sessions: %s." % (arg, _names(everyone)),
        }
    name = _name(hit)
    say = "Now on %s." % name
    if hit.get("last_say"):
        say += " " + _end(hit["last_say"])
    return {"kind": "switch", "title": hit["title"], "name": name, "say": say}


def _list(rows, now: float) -> dict:
    listed = _listed(rows)
    if not listed:
        return {"kind": "list", "say": "There are no sessions."}
    parts = []
    for r in listed[:MAX_LIST]:
        ss = session_state(r, now)
        word = "waiting on you" if ss["reason"] == "question" else _state_word(ss)
        parts.append("%s (%s)" % (_name(r), word))
    more = len(listed) - MAX_LIST
    tail = ", and %d more" % more if more > 0 else ""
    return {"kind": "list", "say": "Sessions: %s%s." % (", ".join(parts), tail)}


def _needs(rows, now: float) -> dict:
    bell = [r for r in _listed(rows) if session_state(r, now)["in_bell"]]
    bell.sort(key=lambda r: _attn_rank(r, now))
    lines = []
    for r in bell[:MAX_NEEDS]:
        ss = session_state(r, now)
        if ss["reason"] == "question":
            lines.append(
                _end("%s is asking: %s" % (_name(r), r.get("last_say") or "a question"))
            )
        else:
            lines.append(_end("%s is %s" % (_name(r), _state_word(ss))))
    return {"kind": "needs", "say": " ".join(lines) if lines else "Nothing needs you."}


def _status(session: str, rows, now: float) -> dict:
    if not session:
        return {"kind": "status", "say": "No session is open."}
    row = _row(rows, session)
    if row is None:
        return {"kind": "status", "say": "%s is not listed." % session}
    ss = session_state(row, now)
    since = _num(row.get("activity_since"))
    mins = round((now - since) / 60) if since else 0
    dur = ""
    if row.get("activity") == "working" and mins >= 1:
        dur = " for %d minute%s" % (mins, "" if mins == 1 else "s")
    say = "%s is %s%s." % (_name(row), _state_word(ss), dur)
    if row.get("last_turn"):
        say += " " + _end(strip_markdown(row["last_turn"]))
    return {"kind": "status", "say": say}


def answer(text, session: str, rows, now: Optional[float] = None) -> dict:
    """The answer to ``text`` said to ``session`` (a title, ``""`` for none).

    ``new`` comes back as :func:`command` parsed it — the route resolves it
    with :func:`plan_new` and creates the session. Every other kind is final."""
    cmd = command(text)
    if cmd is None:
        return {"kind": "prompt"}
    now = time.time() if now is None else now
    kind = cmd["kind"]
    if kind in LOCAL_KINDS:
        return {"kind": kind}
    if kind == "new":
        return cmd
    if kind == "switch":
        return _switch(cmd["arg"], rows)
    if kind == "list":
        return _list(rows, now)
    if kind == "needs":
        return _needs(rows, now)
    return _status(session, rows, now)


# --- new sessions --------------------------------------------------------------
def _compact(text) -> str:
    return re.sub(r"[^a-z0-9]", "", norm(text))


def match_repo(words: List[str], repos) -> Optional[dict]:
    """The repository the spoken ``words`` name, or None.

    ``repos``: ``[{"name", "path", …}]``, most recent first. Spacing, case
    and punctuation don't count ("site check bot 5" is ``sitecheck-bot5``)
    and "the … repo" is not part of a name. An exact name wins, else a name
    the words begin (three letters at least) — the most recent on a tie."""
    ws = list(words)
    while ws and ws[0] in _REPO_LEAD:
        ws = ws[1:]
    while ws and ws[-1] in _REPO_TAIL:
        ws = ws[:-1]
    q = "".join(ws)
    if not q:
        return None
    known = [r for r in repos or () if isinstance(r, dict) and r.get("path")]
    for r in known:
        if _compact(r.get("name")) == q:
            return r
    if len(q) >= 3:
        for r in known:
            if _compact(r.get("name")).startswith(q):
                return r
    return None


def title_base(name: str, task: str) -> str:
    """The title before it is made unique: the ``called`` name hyphenated,
    else the task's first :data:`TITLE_WORDS` words the way the New dialog
    names a session from its task (``title_from_prompt``), else
    :data:`DEFAULT_TITLE`."""
    if name:
        out = "-".join(re.findall(r"[a-z0-9]+", name.lower()))[:TITLE_MAX].strip("-")
        if out:
            return out
    if task:
        out = "-".join(title_from_prompt(task).split("-")[:TITLE_WORDS])
        if out:
            return out
    return DEFAULT_TITLE


def _repo_names(repos, n: int = MAX_NAMES) -> str:
    seen: List[str] = []
    for r in repos or ():
        nm = str(r.get("name") or "")
        if nm and nm not in seen:
            seen.append(nm)
    return ", ".join(seen[:n])


def _which_repo(repos) -> dict:
    say = "I don't know which repository: say new session in <repo>."
    names = _repo_names(repos)
    if names:
        say = "I don't know which repository: say new session in %s." % names
    return {"kind": "new-failed", "say": say}


def plan_new(text, session: str, rows, repos) -> dict:
    """``text`` (a ``new`` phrase) → what to create: ``{"kind": "new",
    "title_base", "name", "repo_name", "repo_path", "task"}``, or
    ``{"kind": "new-failed", "say"}``; ``{"kind": "prompt"}`` when ``text`` is
    no ``new`` phrase after all.

    The repository: the one named — the longest run of the words after
    "in/for/on" that names a known repository, the rest of them starting the
    task ("in sitecheck add tests"); else the current session's (a local
    row); else the only one known, else the most recent. Words after "for"
    or "on" that name no repository were the task ("new session for the
    login bug"); after "in" they were meant as one, and nothing is guessed.
    """
    w = _Words(text)
    p = _parse_new(w)
    if p is None:
        return {"kind": "prompt"}
    name = " ".join(w.words[p["name"][0] : p["name"][1]]) if p["name"] else ""
    task_at = p["task"]
    repo = None
    if p["repo"] is not None:
        a, b = p["repo"]
        for k in range(b - a, 0, -1):
            repo = match_repo(w.words[a : a + k], repos)
            if repo is not None:
                if a + k < b:
                    task_at = a + k  # "in sitecheck add tests …": the rest is the task
                break
        if repo is None:
            if p["prep"] == "in":
                said = w.original(a, b)
                names = _repo_names(repos)
                return {
                    "kind": "new-failed",
                    "say": "I don't know a repository called %s.%s"
                    % (said, " Repositories: %s." % names if names else ""),
                }
            task_at = a - 1  # "for the login bug" / "on the parser" is the task
    if repo is None:
        cur = _row(rows, session)
        if cur is not None and not cur.get("device") and cur.get("path"):
            repo = {"name": cur.get("repo") or "", "path": cur["path"]}
    if repo is None:
        known = [r for r in repos or () if isinstance(r, dict) and r.get("path")]
        recent = [r for r in known if r.get("source") in ("recent", "session")]
        if len(known) == 1:
            repo = known[0]
        elif recent:
            repo = recent[0]
    if repo is None:
        return _which_repo(repos)
    task = w.original(task_at) if task_at is not None else ""
    path = str(repo["path"])
    repo_name = str(repo.get("name") or "") or path.rstrip("/").rsplit("/", 1)[-1]
    return {
        "kind": "new",
        "title_base": title_base(name, task),
        "name": name,
        "repo_name": repo_name,
        "repo_path": path,
        "task": task,
    }


def started(title: str, repo_name: str) -> dict:
    """The answer once ``title`` was created in ``repo_name``."""
    return {
        "kind": "new",
        "title": title,
        "name": title,
        "say": "Started %s in %s." % (title, repo_name),
    }
