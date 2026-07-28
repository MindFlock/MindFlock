"""Session Templates addon: reusable session recipes.

A template bundles the New-session inputs a user repeats — program, repo,
provisioning, and a seed prompt — under a name, so a power user launches a
familiar run in one click instead of re-filling the dialog. It's opt-in (a
sidebar bar newcomers can ignore): calm by default, power as you get
comfortable.

Storage is a small JSON file (``~/.mindflock/session_templates.json``), atomic
writes, tolerant of a missing/corrupt file — the same pattern as
``web/core/prompt_queue.py``. This addon ONLY stores templates; launching a
session stays with the existing ``POST /api/instances`` (the frontend posts the
template's fields there), so session creation keeps one code path.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend.config.config import GetConfigDir

from .base import Addon, AppContext, FrontendDescriptor

_FileName = "session_templates.json"
_LOCK = threading.Lock()
# Cap so a runaway client can't grow the file without bound.
_MAX_TEMPLATES = 100
_VALID_STRATEGIES = ("worktree", "clone")
# Sanity backstops so a bulk import can't bloat the store (rejected, not
# silently truncated — a truncated prompt would corrupt intent).
_MAX_NAME = 100
_MAX_PROMPT = 20000
_MAX_FIELD = 1000  # program / repo_path

# The New-session payload subset a template carries. Anything else in a POST
# body is ignored, so the stored shape stays stable as the create endpoint grows.
_STR_FIELDS = ("program", "repo_path", "prompt", "workspace_strategy")
_BOOL_FIELDS = ("provisioned", "in_place", "init_repo")


def templates_path() -> str:
    """Path to the template store.

    Honors ``$MINDFLOCK_TEMPLATES_FILE`` (tests point it at a tmp file);
    otherwise ``<config dir>/session_templates.json``.
    """
    env = os.environ.get("MINDFLOCK_TEMPLATES_FILE")
    if env:
        return env
    return os.path.join(GetConfigDir(), _FileName)


def _load() -> List[dict]:
    try:
        with open(templates_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    items = data.get("templates") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def _save(items: List[dict]) -> None:
    path = templates_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = json.dumps({"templates": items}, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".", prefix=".tpl.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _clean(body: dict) -> dict:
    """Coerce a POST body to the stored template shape (known fields only)."""
    out = {"name": str(body.get("name", "") or "").strip()}
    for f in _STR_FIELDS:
        out[f] = str(body.get(f, "") or "").strip()
    for f in _BOOL_FIELDS:
        out[f] = bool(body.get(f, False))
    if out["workspace_strategy"] not in _VALID_STRATEGIES:
        out["workspace_strategy"] = "worktree"
    return out


def list_templates() -> List[dict]:
    with _LOCK:
        return _load()


def save_template(body: dict) -> dict:
    """Upsert a template by name (case-insensitive match). Returns the saved one."""
    tpl = _clean(body)
    with _LOCK:
        items = _load()
        key = tpl["name"].lower()
        replaced = False
        for i, existing in enumerate(items):
            if str(existing.get("name", "")).lower() == key:
                items[i] = tpl
                replaced = True
                break
        if not replaced:
            items.append(tpl)
        if len(items) > _MAX_TEMPLATES:
            items = items[-_MAX_TEMPLATES:]
        _save(items)
    return tpl


def delete_template(name: str) -> bool:
    key = (name or "").strip().lower()
    with _LOCK:
        items = _load()
        kept = [t for t in items if str(t.get("name", "")).lower() != key]
        if len(kept) == len(items):
            return False
        _save(kept)
    return True


class TemplatesAddon(Addon):
    id = "templates"
    label = "Templates"

    def __init__(self, ctx: Optional[AppContext] = None) -> None:
        super().__init__(ctx)
        self._router = self._build_router()

    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/templates")

        @router.get("")
        def get_templates() -> JSONResponse:
            return JSONResponse({"templates": list_templates()})

        @router.post("")
        def post_template(body: dict) -> JSONResponse:
            body = body or {}
            name = str(body.get("name", "") or "").strip()
            if not name:
                return JSONResponse(
                    {"error": "template name is required"}, status_code=400
                )
            strategy = str(body.get("workspace_strategy", "") or "").strip()
            if strategy and strategy not in _VALID_STRATEGIES:
                return JSONResponse(
                    {"error": "workspace_strategy must be 'worktree' or 'clone'"},
                    status_code=400,
                )
            if len(name) > _MAX_NAME:
                return JSONResponse(
                    {"error": f"name too long (max {_MAX_NAME} chars)"}, status_code=400
                )
            if len(str(body.get("prompt", "") or "")) > _MAX_PROMPT:
                return JSONResponse(
                    {"error": f"prompt too long (max {_MAX_PROMPT} chars)"},
                    status_code=400,
                )
            for f in ("program", "repo_path"):
                if len(str(body.get(f, "") or "")) > _MAX_FIELD:
                    return JSONResponse(
                        {"error": f"{f} too long (max {_MAX_FIELD} chars)"},
                        status_code=400,
                    )
            saved = save_template(body)
            return JSONResponse({"template": saved, "templates": list_templates()})

        @router.delete("/{name}")
        def del_template(name: str) -> JSONResponse:
            return JSONResponse({"deleted": delete_template(name)})

        return router

    @property
    def router(self) -> APIRouter:
        return self._router

    def frontend(self) -> List[FrontendDescriptor]:
        return [
            FrontendDescriptor(
                id="templates",
                label="Templates",
                where="dialog",
                module="/addons/templates.js",
                api_base="/api/templates",
                order=35,
                # where="dialog": no sidebar bar. slots.js still imports the
                # module (it keys on `module`, not `where`), which builds the
                # modal and exposes window.mindflockAddons.templates.open().
                # Templates are surfaced from the + New dialog.
            )
        ]
