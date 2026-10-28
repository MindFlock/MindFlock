"""Voice mode: ``/api/instances/{title}/voice*`` — talking to a session from
the phone.

The phone page (``/m``) dictates into a session over the existing ``/send``
route and, when the turn ends, speaks the line the session itself wrote
(``core.spoken``). These routes own the per-session switch and the on-demand
condensation:

* ``GET  /api/instances/{title}/voice`` → ``{on, say, has_question, source,
  ts}`` — the flag (``core.voice``) plus the current spoken record.
* ``POST /api/instances/{title}/voice {on: bool}`` → the same shape. Turning
  it ON for a CLI whose prompt hook injects the instruction (Claude) re-runs
  the provider's ``install_activity_hooks`` so a session launched by an
  older build gets the voice-aware hook text — Claude Code hot-reloads its
  hooks file, no relaunch. Turning it off removes the flag file. The flag
  asks nothing of the session: each spoken prompt asks for its own ``Say:``
  line with ``/send``'s ``say`` (the one-shot ``.next`` the hook consumes).
* ``POST /api/instances/{title}/voice/condense`` → ``{text, source:
  "model"}`` — ONE headless model call (``core.commit_message.pick_argv`` +
  ``_run``, the ``/api/session-plan`` pattern) condensing the newest reply
  into at most two plain sentences. Never cached, never taken on a poll:
  this is the step that costs money, and it runs only when the listener asks
  ("summarize that"). 409 ``{error}`` with no reply or no CLI, 504 on
  timeout, 502 when the CLI fails.
* ``POST /api/voice/route {text, session, title?, pending?}`` → ``{kind, …}``
  — the voice router (``core.voice_router``): one finished phrase said to
  ``session`` (a title, ``""`` for none) is either a prompt for that session
  (``{kind: "prompt"}``, the default) or a command: ``switch`` / ``ambiguous``
  / ``no-session``, ``list``, ``needs``, ``status``, ``read`` (the last
  reply, on demand) (each with the ``say`` the client speaks), ``new`` /
  ``ask`` / ``new-failed``, ``switch_index`` /
  ``switch_rel`` (the client's own numbering: it maps ``index`` / ``delta``),
  ``answer`` (a session's dialog: ``dialog_id`` + ``keys`` the client posts
  to ``/answer`` — nothing is pressed here — or ``no_dialog``), ``question``
  (its dialog, or a question it asked in prose, read out), or one the client carries out itself (``cancel``,
  ``stop``, ``hush``, ``off``, ``repeat``, ``condense``; no ``say``) —
  ``stop`` and ``hush`` are the person's ``voice.words``. An optional
  ``title`` maps a numbered target the client resolved ("session three,
  yes" → its title) for the full answer. The rows are ``GET
  /api/instances``'s own (other devices' ``<device>::<title>`` rows
  included), read only once the phrase IS a command (or answers a
  ``pending``). ``new`` creates the session the way the New dialog does
  (``core.session_create``, the task as its first prompt) in the named
  repository (the only one known needs no name), turns its voice flag on
  and arms the one-shot ``.next`` so its seeded first prompt asks for a
  ``Say:`` line — a phrase without a project or a task is an ``ask``
  first. The dialog is ``GET …/dialog?quiet=1``'s own (``_read_dialog``), read only for an
  answer or question naming a local session — and for any other phrase said
  to ``session`` while its agent shows Claude's question tool (``/dialog``'s
  ``kind: "select"``): "red", "one and three", "next", "submit", the words
  for its free-text row are that question's ``answer`` (``keys``, plus
  ``text`` to type first for a free-text answer). An answer that asks
  something back carries ``pending`` — ``ambiguous``'s ``{kind: "pick",
  candidates: [{title, name}]}`` ("Which one: a or b?"), ``ask``'s ``{kind:
  "new", await: "repo" | "task", …the fields gathered}`` ("In which
  project?", "What should it do?") — and the client sends it back with the
  next phrase (for ~30 s): a phrase that answers it is the pick, the
  project or the task (the session is created once nothing is missing); any
  other is routed as usual. Always 200 — a failed create is ``{kind:
  "new-failed", say}``; 400 only for a body that is not an object.

Routes only translate HTTP into core calls; the registry lookup, the tmux
name and the providers are read through the server module at call time, so
tests that patch ``server._x`` reach them.
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.session import tmux
from backend.web.core import agent_io as _agent_io
from backend.web.core import auth as _auth
from backend.web.core import commit_message as _commit_message
from backend.web.core import session_create as _session_create
from backend.web.core import spoken as _spoken
from backend.web.core import voice as _voice
from backend.web.core import voice_router as _voice_router
from backend.web.core import voice_words as _voice_words
from backend.web.routes import _server

router = APIRouter()

#: Timeout for the one-shot condensation (seconds).
CONDENSE_TIMEOUT_S = 45.0
#: The newest reply is cut to this many characters (its tail) before it is
#: handed to the model — the whole prompt travels as ONE argv token.
CONDENSE_INPUT_MAX = 6000
#: Appended to the reply text.
CONDENSE_SUFFIX = (
    "Condense this for someone listening, not reading: at most two short plain "
    "sentences, no code, paths or markdown; include any question for them."
)
#: Ceiling on what the condensation returns (characters).
CONDENSE_OUTPUT_MAX = 600
_NO_REPLY = "nothing to condense — the session has not replied yet"


def _workdir(inst) -> str:
    try:
        return inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        return ""


def _provider(inst):
    return _server().providers.resolve(getattr(inst, "Program", "") or "")


def _body(title: str, inst) -> dict:
    """``{on, say, has_question, source, ts}`` for ``title``."""
    name = tmux.to_mindflock_tmux_name(title)
    sp = _spoken.spoken_for(title, inst)
    return {
        "on": _voice.is_on(name),
        "say": sp.get("say"),
        "has_question": bool(sp.get("has_question")),
        "source": sp.get("source") or "none",
        "ts": sp.get("ts"),
    }


@router.get("/api/instances/{title}/voice")
async def voice_get(title: str) -> JSONResponse:
    """The session's voice state → ``{on, say, has_question, source, ts}``;
    404 unknown session."""
    inst, err = _server()._inst_or_404(title)
    if err is not None:
        return err
    return JSONResponse(await asyncio.to_thread(_body, title, inst))


def _set(title: str, inst, on: bool) -> dict:
    name = tmux.to_mindflock_tmux_name(title)
    _voice.set_on(name, on)
    if on:
        # A running Claude session launched by an older build still carries
        # the pre-voice hook text; re-pinning installs the Stop/UserPrompt-
        # Submit branches and the CLI hot-reloads them. Idempotent (the merge
        # skips an unchanged file) and best-effort, like every hook install.
        try:
            prov = _provider(inst)
            if prov.voice_prompt_hook():
                wt = _workdir(inst)
                if wt:
                    prov.install_activity_hooks(wt, name)
        except Exception:  # noqa: BLE001 — the flag is set; hooks are enrichment
            pass
    return _body(title, inst)


@router.post("/api/instances/{title}/voice")
async def voice_set(title: str, request: Request) -> JSONResponse:
    """Turn voice mode on or off: ``{"on": bool}`` → ``{on, say, has_question,
    source, ts}``. 400 when the body is not an object with a boolean ``on``,
    404 unknown session."""
    inst, err = _server()._inst_or_404(title)
    if err is not None:
        return err
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — not JSON: a bad body, not a 422
        payload = None
    on = payload.get("on") if isinstance(payload, dict) else None
    if not isinstance(on, bool):
        return JSONResponse({"error": "on must be true or false"}, status_code=400)
    try:
        body = await asyncio.to_thread(_set, title, inst, on)
    except OSError as oerr:
        return JSONResponse(
            {"error": "could not write the voice flag: %s" % oerr}, status_code=500
        )
    return JSONResponse(body)


def _newest_reply_text(inst, title: str) -> str:
    """The newest reply for ``title``: the provider's last assistant text,
    else the reply file's text, else the pane's visible screen; ``""`` when
    there is nothing."""
    srv = _server()
    name = tmux.to_mindflock_tmux_name(title)
    text = ""
    try:
        got = _provider(inst).last_assistant_text(name, _workdir(inst))
        text = got if isinstance(got, str) else ""
    except Exception:  # noqa: BLE001
        text = ""
    if not text.strip():
        reply = _spoken.read_reply(name)
        text = reply[1] if reply else ""
    if not text.strip():
        live = srv._live_session_name(name)
        if live is not None:
            screen, cap_err = _agent_io.capture_screen(live)
            text = screen if cap_err is None and screen else ""
    return text or ""


def _run_oneshot(prompt: str, program: str) -> str:
    """Ask the session's CLI (else the flock's default) ``prompt`` headlessly
    and return its stdout. Raises ``CommitMessageError`` with a reason.
    Module-level so tests replace it."""
    argv = _commit_message.pick_argv(prompt, program, "", purpose="a spoken summary")
    # cwd is commit_message's neutral dir, for its reasons: the question needs
    # no repo context (the worktree's AGENTS.md would land in a prompt whose
    # answer is read aloud), and $HOME would make `claude -p` file its
    # transcript under ~/.claude/projects and read ~/CLAUDE.md.
    return _commit_message._run(argv, _commit_message.neutral_cwd(), CONDENSE_TIMEOUT_S)


def _condense(inst, title: str):
    """``(body, status)`` for the condense route (blocking)."""
    text = _newest_reply_text(inst, title)
    if not text.strip():
        return {"error": _NO_REPLY}, 409
    text = text[-CONDENSE_INPUT_MAX:]
    prompt = text.rstrip() + "\n\n" + CONDENSE_SUFFIX
    try:
        out = _run_oneshot(prompt, getattr(inst, "Program", "") or "")
    except _commit_message.CommitMessageError as err:
        reason = str(err)
        if "did not answer within" in reason:
            return {"error": reason}, 504
        if "not installed" in reason or "no installed CLI" in reason:
            return {"error": reason}, 409
        return {"error": reason}, 502
    said = _spoken.strip_markdown(out)
    if len(said) > CONDENSE_OUTPUT_MAX:
        said = said[: CONDENSE_OUTPUT_MAX - 1].rstrip() + "…"
    if not said:
        return {"error": "the model returned nothing to say"}, 502
    return {"text": said, "source": "model"}, 200


@router.post("/api/instances/{title}/voice/condense")
async def voice_condense(title: str) -> JSONResponse:
    """Condense the newest reply for a listener → ``{text, source: "model"}``.
    409 ``{error}`` with no reply or no CLI, 504 on timeout, 502 when the CLI
    fails. Never cached."""
    inst, err = _server()._inst_or_404(title)
    if err is not None:
        return err
    body, status = await asyncio.to_thread(_condense, inst, title)
    return JSONResponse(body, status_code=status)


# ---- the voice router ---------------------------------------------------------
#: A create error is read out: at most this many characters of it.
NEW_REASON_MAX = 120


def _session_rows(request) -> list:
    """``GET /api/instances``'s rows, exactly as it serves them (its tick
    snapshot, renames, other devices' rows). Blocking; module-level so tests
    replace it."""
    resp = _server().list_instances(request)
    try:
        rows = json.loads(resp.body)
    except Exception:  # noqa: BLE001 — the listing always answers a JSON list
        rows = []
    return rows if isinstance(rows, list) else []


def _known_repos(rows: list) -> list:
    """``[{name, path, source}]`` most recent first: what the New dialog
    offers (``/api/repos/suggest``) plus every local session's repository,
    deduplicated by real path. Blocking (a shallow folder sweep)."""
    srv = _server()
    out: list = []
    seen: set = set()

    def add(path: str, name: str, source: str) -> None:
        try:
            real = os.path.realpath(os.path.expanduser(path))
        except Exception:  # noqa: BLE001
            return
        if not path or real in seen:
            return
        seen.add(real)
        out.append(
            {"name": name or os.path.basename(real), "path": path, "source": source}
        )

    try:
        suggested = srv.suggest_repos(
            recent_paths=srv._recent_repo_paths(), cwd=os.getcwd()
        )
    except Exception:  # noqa: BLE001 — the sessions' own repos still count
        suggested = []
    for s in suggested or ():
        if isinstance(s, dict):
            add(
                str(s.get("path") or ""),
                str(s.get("name") or ""),
                str(s.get("source") or ""),
            )
    for r in rows:
        if isinstance(r, dict) and not r.get("device") and not r.get("pending"):
            add(str(r.get("path") or ""), str(r.get("repo") or ""), "session")
    return out


async def _create_session(payload: dict, person: str):
    """``(status, body)`` of the New dialog's create (``POST /api/instances``
    in process). Module-level so tests replace it."""
    return await _session_create.create_result(payload, person=person)


def _failed(reason: str) -> dict:
    reason = " ".join(str(reason or "it failed").split()).rstrip(".")
    if len(reason) > NEW_REASON_MAX:
        reason = reason[: NEW_REASON_MAX - 1].rstrip() + "…"
    return {"kind": "new-failed", "say": "Couldn't start it: %s." % reason}


async def _start_new(text: str, session: str, rows: list, request) -> dict:
    """Resolve and create the session a ``new`` phrase asks for — or the
    question it asks first (``ask``: the project, the task)."""
    repos = await asyncio.to_thread(_known_repos, rows)
    plan = _voice_router.plan_new(text, session, rows, repos)
    if plan.get("kind") != "new":
        return plan
    return await _create_planned(plan, request)


async def _create_planned(plan: dict, request) -> dict:
    """Create the session a finished plan names (``core.voice_router``'s
    ``kind: "new"``) → ``started``, or ``new-failed``."""
    title = await asyncio.to_thread(_session_create.free_title, plan["title_base"])
    payload = {"title": title, "repo_path": plan["repo_path"]}
    if plan["task"]:
        payload["prompt"] = plan["task"]
    try:
        status, body = await _create_session(payload, _auth.request_person(request))
    except Exception as err:  # noqa: BLE001 — the client speaks why
        return _failed(str(err))
    if status >= 300:
        return _failed(body.get("error") or "HTTP %d" % status)
    title = str(body.get("title") or title)
    try:
        # The person is talking to it: the voice marker goes on, and the
        # seeded first prompt asks for its Say: line the way a dictated
        # /send {say: true} does — the one-shot .next the prompt hook
        # consumes (a CLI without that hook gets no line on this first
        # prompt; its later dictated prompts do, typed by /send).
        name = tmux.to_mindflock_tmux_name(title)
        _voice.set_on(name, True)
        if plan["task"]:
            _voice.arm_next(name)
    except Exception:  # noqa: BLE001 — the session exists; voice is enrichment
        pass
    return _voice_router.started(title, plan["repo_name"])


async def _read_dialog(title: str):
    """The dialog ``title``'s agent is blocked on — ``GET
    /api/instances/{title}/dialog?quiet=1``'s own body, the same reader and
    gate (the provider parses the visible screen) — or None when it is not
    asking (204), is gone or can't be read. Module-level so tests replace
    it."""
    try:
        resp = await _server().instance_dialog(title, quiet="1")
    except Exception:  # noqa: BLE001 — unreadable reads as "not asking"
        return None
    if getattr(resp, "status_code", 0) != 200:
        return None
    try:
        body = json.loads(resp.body)
    except Exception:  # noqa: BLE001
        return None
    return body if isinstance(body, dict) else None


def _read_spoken(title: str) -> dict:
    """``{say, has_question}`` — the spoken record ``GET …/voice`` serves.
    Blocking; module-level so tests replace it."""
    inst = _server().ENGINE.instances.get(title)
    if inst is None:
        return {}
    try:
        return _spoken.spoken_for(title, inst) or {}
    except Exception:  # noqa: BLE001
        return {}


async def _finish_answer(ans: dict, rows: list) -> dict:
    """An ``answer`` / ``question`` naming a session, finished with its live
    dialog. Another device's session can't be read here: its row's
    ``asking`` text (else its ``last_say``) stands in for a question, and an
    answer says so. A local session's prose question comes back from
    ``/dialog`` itself (``kind: "question"``)."""
    title = ans["title"]
    row = next((r for r in rows if r.get("title") == title), None) or {}
    if row.get("device") or "::" in title:
        asked = _voice_router.asking_text(row)
        spoken = {
            "say": asked or row.get("last_say"),
            "has_question": bool(asked) or row.get("activity") == "clarify",
        }
        return _voice_router.finish(ans, None, spoken, remote=True)
    dialog = await _read_dialog(title)
    spoken = None
    if ans["kind"] == "question":
        spoken = await asyncio.to_thread(_read_spoken, title)
    return _voice_router.finish(ans, dialog, spoken)


def _words() -> dict:
    """The person's effective voice words (``core.voice_words.effective``):
    the router's ``stop`` / ``hush``. The defaults when the settings can't
    be read. Blocking; module-level so tests replace it."""
    try:
        from backend.config import settings as settings_store

        return _voice_words.effective(settings_store.load_settings())
    except Exception:  # noqa: BLE001 — the router must still answer
        return _voice_words.effective({})


async def _pending_answer(
    text: str, session: str, pending: dict, rows: list, words, request
):
    """The answer to the question the router asked last — "Which one: a or
    b?" (``pending.kind`` "pick") or a new session's "In which project?" /
    "What should it do?" ("new") — or None when ``text`` is not one (it is
    routed as usual)."""
    if pending.get("kind") == "pick":
        return _voice_router.pick(text, pending, rows)
    if pending.get("kind") == "new":
        repos = await asyncio.to_thread(_known_repos, rows)
        plan = _voice_router.continue_new(text, pending, session, rows, repos, words)
        if plan is not None and plan.get("kind") == "new":
            return await _create_planned(plan, request)
        return plan
    return None


#: Commands a phrase said to a session showing Claude's question tool may
#: really be its answer: "go back" (a tab, not the previous session) and, on
#: the review screen, "cancel". A prompt (no command) may be one too.
_SELECT_FIRST = ("switch_rel", "cancel")


async def _select_answer(text: str, session: str, request):
    """The ``answer`` to ``session``'s question tool that ``text`` is
    ("red", "one and three", "next", "submit", the free-text row's words),
    or None — not asking such a question, or not an answer to it."""
    dialog = await _read_dialog(session)
    if not _voice_router.is_select(dialog):
        return None
    if _voice_router.select_answer(text, dialog) is None:
        return None
    rows = await asyncio.to_thread(_session_rows, request)
    return _voice_router.select_route(text, session, dialog, rows)


@router.post("/api/voice/route")
async def voice_route(request: Request) -> JSONResponse:
    """One finished phrase → ``{kind, …}`` (see the module docstring and
    docs/web-api.md). 400 when the body is not an object."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — not JSON: a bad body, not a 422
        payload = None
    if not isinstance(payload, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    text = str(payload.get("text") or "")
    session = str(payload.get("session") or "")
    title = str(payload.get("title") or "")
    pending = payload.get("pending")
    words = await asyncio.to_thread(_words)
    rows = None
    if isinstance(pending, dict) and text.strip():
        rows = await asyncio.to_thread(_session_rows, request)
        got = await _pending_answer(text, session, pending, rows, words, request)
        if got is not None:
            return JSONResponse(got)
    cmd = _voice_router.command(text, words)
    if session and (
        cmd is None or cmd["kind"] in _SELECT_FIRST or cmd.get("mode") == "strict"
    ):
        picked = await _select_answer(text, session, request)
        if picked is not None:
            return JSONResponse(picked)
    if cmd is None:
        return JSONResponse({"kind": "prompt"})
    if cmd["kind"] in _voice_router.LOCAL_KINDS:
        return JSONResponse({"kind": cmd["kind"]})
    if rows is None:
        rows = await asyncio.to_thread(_session_rows, request)
    if cmd["kind"] == "new":
        return JSONResponse(await _start_new(text, session, rows, request))
    ans = _voice_router.answer(text, session, rows, title=title, words=words)
    if ans.get("kind") in ("answer", "question") and ans.get("title"):
        ans = await _finish_answer(ans, rows)
    return JSONResponse(ans)


# ---- voice help: questions about MindFlock -------------------------------------
#: Longest question handed to the docs search and the model (characters).
HELP_TEXT_MAX = 400


def _help_answer(text: str) -> dict:
    """``core.voice_help.answer`` — blocking (the model call). Module-level
    so tests replace it."""
    from backend.web.core import voice_help as _voice_help

    return _voice_help.answer(text)


@router.post("/api/voice/help")
async def voice_help(request: Request) -> JSONResponse:
    """A spoken question about MindFlock (the router's ``help`` answer
    carries it as ``text``) → ``{say, source: {doc, heading} | null, model}``
    — answered from the docs (``core.voice_help``): the best sections worded
    by one quick model call, else the best section's first sentences. 400
    when the body is not an object with a non-empty ``text``."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — not JSON: a bad body, not a 422
        payload = None
    text = payload.get("text") if isinstance(payload, dict) else None
    text = (
        " ".join(str(text or "").split())[:HELP_TEXT_MAX]
        if isinstance(text, str)
        else ""
    )
    if not text:
        return JSONResponse({"error": "text must be a question"}, status_code=400)
    return JSONResponse(await asyncio.to_thread(_help_answer, text))
