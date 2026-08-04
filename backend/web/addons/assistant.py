"""Assistant addon: a long-lived personal ``claude`` session + a todo list.

A single ``claude`` session whose only job is to answer questions and manage a
drag-orderable todo list. It lives in its own directory (kept out of any repo so
its ``claude`` never inherits a project's CLAUDE.md) and edits ``todos.json``
there directly. Exposes ``/api/assistant/terminal`` (interactive chat) and
``/api/assistant/todos`` (REST).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, WebSocket
from fastapi.responses import JSONResponse

from backend import log, providers
from backend.session import tmux

from backend.web.core.terminal import (
    apply_scroll_speed,
    pump_pty,
    spawn_tmux_attach,
    _clear_exit_marker,
    _read_exit_marker,
    _wrap_launch_cmd,
)

from .base import Addon, AppContext, FrontendDescriptor

ASSIST_DIR = Path(
    os.environ.get("MINDFLOCK_ASSISTANT_DIR", str(Path.home() / ".mindflock-assistant"))
)
ASSIST_TODOS = ASSIST_DIR / "todos.json"
ASSIST_CLAUDE_MD = ASSIST_DIR / "CLAUDE.md"
#: The user's OWN instructions live here, separate from CLAUDE.md. The editor
#: reads/writes only this file, so the managed MindFlock seed is never part of
#: the editable surface — it can't be shown in the box or clobbered. CLAUDE.md
#: (what ``claude`` actually reads) is always regenerated as seed + this text.
ASSIST_USER_MD = ASSIST_DIR / "user_instructions.md"
ASSIST_TMUX = tmux.to_mindflock_tmux_name("mindflock_assistant")

_ASSIST_CLAUDE_MD_SEED = """\
# MindFlock Personal Assistant

You are a personal assistant embedded in MindFlock. Your job is simple:

1. **Answer questions** the user asks — concisely and directly.
2. **Manage a todo list** stored in `todos.json` in this directory.
3. **Make Shortcut tickets** when asked, using the Shortcut MCP tools.

## The todo list (`todos.json`)

It is a JSON array. Each item is an object:

```json
{ "id": "a1b2c3d4", "text": "Write the design doc", "done": false }
```

Rules for editing it:

- Read the file, modify the array, write the whole array back. Keep it valid JSON.
- **Preserve each item's `id`** when editing existing items — the UI uses it to
  track rows across reorders. Only invent a new `id` (any short unique string)
  for items you add.
- The **array order is the display order** — the user reorders items by dragging
  in the UI, so don't reshuffle unless they ask you to.
- `done: true` marks an item checked off; leave it in the list unless asked to
  remove it.
- After you change the list, briefly tell the user what you changed.

The UI shows this same list in a "Todo" tab and writes to the same file, so
always re-read before editing to avoid clobbering a change made there.
"""


# The assistant's CLAUDE.md is split into a MANAGED block (the seed above —
# core behavior + the todo-list rules the UI depends on) and a USER block below
# this marker. The Agent-file editor only ever shows/edits the user block, and a
# save always re-writes the managed block verbatim, so a user's custom
# instructions can never break todo-list management.
_USER_MARKER = (
    "<!-- ▼▼▼ YOUR INSTRUCTIONS — MindFlock keeps everything above; edit below ▼▼▼ -->"
)


def _compose_agent_file(user_text: str) -> str:
    """The full CLAUDE.md = managed seed + marker + the user's own instructions."""
    body = (user_text or "").strip()
    out = _ASSIST_CLAUDE_MD_SEED.rstrip() + "\n\n" + _USER_MARKER + "\n"
    if body:
        out += "\n" + body + "\n"
    return out


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file + replace (never a partial file)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _assistant_program() -> str:
    """The CLI the Assistant runs.

    ``coding_cli.assistant_provider`` (Settings → Agent) first, so the Assistant
    can be a different CLI from the one doing the coding, then the same default
    everything else resolves to. This was hardcoded to ``claude``, which made
    the Assistant unusable for anyone who hadn't set Claude up.
    """
    try:
        from backend.config.settings import load_settings

        chosen = (load_settings().coding_cli.assistant_provider or "").strip()
        if chosen:
            return chosen
    except Exception:  # noqa: BLE001 — settings are optional
        pass
    try:
        from backend.config.program import resolve_default_program

        return resolve_default_program() or "claude"
    except Exception:  # noqa: BLE001 — never block the chat on config
        return "claude"


def _seed_assistant_dir() -> None:
    """Ensure the assistant's dir, the user-instructions file, CLAUDE.md and
    todos.json exist and are coherent.

    The user file is the source of truth for the *user's* text. CLAUDE.md is
    then regenerated as
    ``seed + marker + user text`` — regenerated rather than seed-if-missing, so a
    changed managed seed always reaches the assistant and the seed is never part
    of the editable surface. todos.json is seeded once.
    """
    try:
        ASSIST_DIR.mkdir(parents=True, exist_ok=True)
        if not ASSIST_USER_MD.exists():
            _atomic_write(ASSIST_USER_MD, "")
        try:
            user = ASSIST_USER_MD.read_text(encoding="utf-8")
        except OSError:
            user = ""
        composed = _compose_agent_file(user)
        try:
            current = ASSIST_CLAUDE_MD.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current != composed:  # avoid needless rewrites on every read
            _atomic_write(ASSIST_CLAUDE_MD, composed)
        if not ASSIST_TODOS.exists():
            ASSIST_TODOS.write_text("[]\n", encoding="utf-8")
    except Exception as err:  # noqa: BLE001
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("failed to seed assistant dir: %v", err)


def _ensure_assistant_session():
    """Ensure the personal-assistant tmux session exists, starting ``claude`` in
    ASSIST_DIR if it isn't running. Returns ``(name, error_or_None)``.

    Like the agent sessions it ALWAYS restarts on its own; only whether it
    resumes the prior thread changes. A clean quit (Ctrl+C / exit) restarts
    fresh; an unnatural death (kill/crash/no marker) resumes via --continue.
    """
    name = ASSIST_TMUX
    try:
        exists = (
            subprocess.run(
                ["tmux", "has-session", "-t=" + name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
            == 0
        )
    except subprocess.TimeoutExpired:
        return name, "tmux timed out after 10s"
    if exists:
        return name, None
    _seed_assistant_dir()  # re-seed defensively in case the dir was wiped
    program = _assistant_program()
    provider = providers.resolve(program)
    # Natural quit -> fresh; unnatural death -> resume the conversation.
    resume = not provider.is_natural_exit(_read_exit_marker(name))
    cmd = provider.build_launch_command(
        providers.LaunchContext(program=program, resume=resume, session_name=name)
    )
    _clear_exit_marker(name)
    wrapped = _wrap_launch_cmd(cmd, name)
    try:
        created = subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                name,
                "-c",
                str(ASSIST_DIR),
                "sh",
                "-c",
                wrapped,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return name, "tmux new-session timed out after 10s"
    if created.returncode != 0:
        # Race: two clients can ensure the session at once; the loser gets
        # "duplicate session". If it exists now, treat as success.
        try:
            if (
                subprocess.run(
                    ["tmux", "has-session", "-t=" + name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                ).returncode
                == 0
            ):
                return name, None
        except subprocess.TimeoutExpired:
            pass
        return name, created.stderr.decode("utf-8", "replace").strip()
    for opt, val in (
        ("mouse", "on"),
        ("history-limit", "10000"),
        ("window-size", "latest"),
    ):
        try:
            subprocess.run(
                ["tmux", "set-option", "-t", name, opt, val],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            pass  # cosmetic option; never block session creation
    apply_scroll_speed()  # wheel speed (global; re-asserted per server)
    return name, None


def _read_instructions() -> str:
    """The user's OWN instructions — only what they typed, never the managed
    MindFlock seed. Read straight from the user-instructions file (seeded /
    migrated by :func:`_seed_assistant_dir`), so the built-in prompt can never
    appear in the editor."""
    _seed_assistant_dir()
    try:
        return ASSIST_USER_MD.read_text(encoding="utf-8").strip("\n")
    except OSError:
        return ""


def _write_instructions(text: str) -> None:
    """Persist the user's instructions to their own file, then regenerate
    CLAUDE.md = managed seed + this text. The seed is never part of what's
    stored here, so it can't be lost or shown. Applies the next time the
    assistant session starts (claude reads CLAUDE.md at launch)."""
    body = (text or "").strip()
    _atomic_write(ASSIST_USER_MD, (body + "\n") if body else "")
    _seed_assistant_dir()  # regenerates CLAUDE.md from the seed + this user text


def _restart_assistant_session() -> None:
    """Kill the assistant tmux session so the next chat relaunches ``claude``
    (picking up an edited CLAUDE.md). Best-effort; no-op if not running."""
    try:
        subprocess.run(
            ["tmux", "kill-session", "-t=" + ASSIST_TMUX],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        pass  # best-effort; nothing to do if tmux is wedged


def _read_todos() -> list[dict]:
    """Load todos.json as a list of {id, text, done} dicts, tolerating a missing
    or malformed file (returns [])."""
    try:
        raw = ASSIST_TODOS.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "id": str(item.get("id") or "t%d" % i),
                "text": str(item.get("text", "")),
                "done": bool(item.get("done", False)),
            }
        )
    return out


class AssistantAddon(Addon):
    id = "assistant"
    label = "Assistant"

    def __init__(self, ctx: Optional[AppContext] = None) -> None:
        super().__init__(ctx)
        self._router = self._build_router()

    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/assistant")

        @router.websocket("/terminal")
        async def assistant_terminal_ws(ws: WebSocket) -> None:
            await ws.accept()
            # Always (re)launches; _ensure_assistant_session resumes the prior
            # thread only after an unnatural death, fresh after a clean quit.
            name, err = await asyncio.to_thread(_ensure_assistant_session)
            if err is not None:
                await ws.send_text(json.dumps({"type": "error", "message": err}))
                await ws.close(code=4500)
                return
            try:
                proc = spawn_tmux_attach(name)
            except Exception as err:  # noqa: BLE001
                await ws.send_text(json.dumps({"type": "error", "message": str(err)}))
                await ws.close(code=4500)
                return
            await pump_pty(ws, proc, allow_input=True)

        @router.get("/instructions")
        async def get_instructions() -> JSONResponse:
            """The assistant's agent file (CLAUDE.md) — its standing instructions."""
            return JSONResponse({"text": await asyncio.to_thread(_read_instructions)})

        @router.put("/instructions")
        async def put_instructions(payload: dict) -> JSONResponse:
            """Replace the assistant's instructions. Takes effect the next time
            the assistant session (re)starts."""
            text = (payload or {}).get("text")
            if not isinstance(text, str):
                return JSONResponse({"error": "text must be a string"}, status_code=400)
            try:
                await asyncio.to_thread(_write_instructions, text)
            except Exception as err:  # noqa: BLE001
                return JSONResponse({"error": str(err)}, status_code=500)
            return JSONResponse({"text": await asyncio.to_thread(_read_instructions)})

        @router.post("/restart")
        async def restart_assistant() -> JSONResponse:
            """Kill the assistant session so the next chat relaunches with the
            latest CLAUDE.md (used by 'Save & restart')."""
            await asyncio.to_thread(_restart_assistant_session)
            return JSONResponse({"ok": True})

        @router.get("/todos")
        async def get_todos() -> JSONResponse:
            return JSONResponse({"todos": await asyncio.to_thread(_read_todos)})

        @router.put("/todos")
        async def put_todos(payload: dict) -> JSONResponse:
            """Replace the whole list (the UI sends the full, reordered array).
            Normalizes each row to {id, text, done} and writes todos.json
            atomically."""
            items = (payload or {}).get("todos")
            if not isinstance(items, list):
                return JSONResponse({"error": "todos must be a list"}, status_code=400)
            clean = []
            seen = set()
            for i, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                tid = str(item.get("id") or "").strip() or "t%d" % i
                while tid in seen:  # keep ids unique even if the client sent dupes
                    tid += "_"
                seen.add(tid)
                clean.append(
                    {
                        "id": tid,
                        "text": str(item.get("text", "")),
                        "done": bool(item.get("done", False)),
                    }
                )

            def _write() -> None:
                _seed_assistant_dir()
                _atomic_write(ASSIST_TODOS, json.dumps(clean, indent=2) + "\n")

            try:
                await asyncio.to_thread(_write)
            except Exception as err:  # noqa: BLE001
                return JSONResponse({"error": str(err)}, status_code=500)
            return JSONResponse({"todos": clean})

        return router

    @property
    def router(self) -> APIRouter:
        return self._router

    # --- lifecycle -------------------------------------------------------- #
    async def on_startup(self, ctx: AppContext) -> None:
        # Seed-if-missing; moved from import time to startup. Idempotent.
        await asyncio.to_thread(_seed_assistant_dir)

    # --- frontend --------------------------------------------------------- #
    def frontend(self):
        return [
            FrontendDescriptor(
                id="assistant",
                label="Assistant",
                where="sidebar-bar",
                module=None,  # hand-wired in app.js/index.html; no ES module
                api_base="/api/assistant",
                ws_path="/api/assistant/terminal",
                order=20,
                builtin_ui=True,  # keeps its bespoke sidebar bar in app.js
            )
        ]
