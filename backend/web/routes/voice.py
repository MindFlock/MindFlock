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
  hooks file, no relaunch. Turning it off removes the flag file.
* ``POST /api/instances/{title}/voice/condense`` → ``{text, source:
  "model"}`` — ONE headless model call (``core.commit_message.pick_argv`` +
  ``_run``, the ``/api/session-plan`` pattern) condensing the newest reply
  into at most two plain sentences. Never cached, never taken on a poll:
  this is the step that costs money, and it runs only when the listener asks
  ("summarize that"). 409 ``{error}`` with no reply or no CLI, 504 on
  timeout, 502 when the CLI fails.

Routes only translate HTTP into core calls; the registry lookup, the tmux
name and the providers are read through the server module at call time, so
tests that patch ``server._x`` reach them.
"""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.session import tmux
from backend.web.core import agent_io as _agent_io
from backend.web.core import commit_message as _commit_message
from backend.web.core import spoken as _spoken
from backend.web.core import voice as _voice
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
    # cwd is $HOME, like the session plan's one-shot: the question needs no
    # repo context, and running inside the worktree would feed its AGENTS.md
    # into a prompt whose answer is read aloud.
    return _commit_message._run(argv, os.path.expanduser("~"), CONDENSE_TIMEOUT_S)


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
