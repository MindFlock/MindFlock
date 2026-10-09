"""Onboarding addon: the first-run plan, Connect GitHub, a new computer in one line.

API-only (Setup and Settings → Devices draw it). Three groups of routes:

* ``/api/onboarding`` — the ordered first-run plan (:mod:`backend.onboarding`)
  and Setup's "First computer, or join one you already have?" answer.
* ``/api/github/*`` — Connect GitHub (:mod:`backend.web.core.github_auth`):
  status, the device flow, a pasted token, ``gh auth login --web`` in a login
  terminal, the git identity and push credential.
* ``/api/fleet/bootstrap`` and ``/api/fleet/readiness*`` — the one-line
  install for a brand-new computer (:mod:`backend.web.core.bootstrap`) and
  each member's own readiness summary (:mod:`backend.web.core.readiness`).
  Separate from the fleet addon so the roster routes stay as they are.

Everything that writes — a token, the git config, a sign-in terminal, an
invite — is for the person AT this device only (``auth.privileged``: never a
relayed request, never an anonymous caller of an exposed gate-off server);
403 otherwise. ``readiness/self`` is member-to-member: the fleet key.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse

from .base import Addon, AppContext

_FORBIDDEN = {"error": "do this on the device itself (or a signed-in one)"}

#: How long a doctor run is reused by the plan (Setup re-asks often).
_CHECKS_TTL = 20.0


async def _privileged(scope) -> bool:
    try:
        from backend.web.core import auth as web_auth

        return bool(await web_auth.privileged(scope))
    except Exception:  # noqa: BLE001 — fail closed
        return False


def _known_repos() -> set:
    """Real paths a push check may run in: the remembered repo
    (``general.last_repo_path``) and every session's folder and worktree.
    Never raises."""
    out = set()
    try:
        from backend.web.core import github_auth

        last = github_auth.remembered_repo()
        if last:
            out.add(os.path.realpath(os.path.expanduser(last)))
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.web import server

        for inst in list(server.ENGINE.instances.values()):
            try:
                wt = inst.GetWorktreePath() or ""
            except Exception:  # noqa: BLE001
                wt = ""
            for p in (wt, getattr(inst, "Path", "") or ""):
                if p:
                    out.add(os.path.realpath(p))
    except Exception:  # noqa: BLE001
        pass
    return out


def _forbidden() -> JSONResponse:
    return JSONResponse(_FORBIDDEN, status_code=403)


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization") or ""
    return raw[7:].strip() if raw.lower().startswith("bearer ") else ""


def _changed() -> None:
    """Something a plan or readiness summary reads just changed here."""
    try:
        from backend.web.core import readiness

        readiness.invalidate()
    except Exception:  # noqa: BLE001
        pass


class OnboardingAddon(Addon):
    id = "onboarding"
    label = "Onboarding"

    def __init__(self, ctx: Optional[AppContext] = None) -> None:
        super().__init__(ctx)
        self._checks: Optional[list] = None
        self._checks_at = 0.0
        self._router = self._build_router()

    @property
    def router(self) -> APIRouter:
        return self._router

    def _doctor_checks(self, refresh: bool) -> list:
        now = time.monotonic()
        if refresh or self._checks is None or now - self._checks_at > _CHECKS_TTL:
            from backend import doctor

            if refresh:
                from backend import pathenv

                pathenv.refresh()
            self._checks = [c.to_dict() for c in doctor.run_checks()]
            self._checks_at = now
        return list(self._checks)

    def _plan(self, refresh: bool) -> dict:
        from backend import onboarding

        return onboarding.build_plan(
            onboarding.collect(checks=self._doctor_checks(refresh))
        )

    def _build_router(self) -> APIRouter:  # noqa: C901 — one flat route table
        router = APIRouter(prefix="/api")

        # ---------------------------------------------------------------- #
        # the plan
        # ---------------------------------------------------------------- #
        @router.get("/onboarding")
        async def get_onboarding(
            request: Request, refresh: bool = False
        ) -> JSONResponse:
            plan = await asyncio.to_thread(self._plan, refresh)
            if not await _privileged(request.scope):
                for step in plan["steps"]:  # who you are on GitHub: yours only
                    if step["id"] == "github" and step["status"] == "ok":
                        step["reason"] = "connected"
            return JSONResponse(plan)

        @router.post("/onboarding/choice")
        async def post_choice(request: Request) -> JSONResponse:
            if not await _privileged(request.scope):
                return _forbidden()
            from backend import onboarding

            body = await _json(request)
            try:
                choice = await asyncio.to_thread(
                    onboarding.set_choice, str(body.get("choice") or "")
                )
            except ValueError as err:
                return JSONResponse({"error": str(err)}, status_code=400)
            return JSONResponse(
                {"choice": choice, **(await asyncio.to_thread(self._plan, False))}
            )

        # ---------------------------------------------------------------- #
        # Connect GitHub
        # ---------------------------------------------------------------- #
        @router.get("/github/status")
        async def github_status(request: Request) -> JSONResponse:
            from backend.web.core import github_auth

            st = await asyncio.to_thread(github_auth.status)
            if not await _privileged(request.scope):
                st = {"connected": st["connected"], "source": st["source"]}
            return JSONResponse(st)

        @router.post("/github/device/start")
        async def device_start(request: Request) -> JSONResponse:
            if not await _privileged(request.scope):
                return _forbidden()
            from backend.web.core import github_auth

            out = await asyncio.to_thread(github_auth.start_device_flow)
            return JSONResponse(out, status_code=200 if out.get("ok") else 409)

        @router.post("/github/device/poll")
        async def device_poll(request: Request) -> JSONResponse:
            if not await _privileged(request.scope):
                return _forbidden()
            from backend.web.core import github_auth

            out = await asyncio.to_thread(github_auth.poll_device_flow)
            if out.get("state") == "done":
                _changed()
            return JSONResponse(out)

        @router.delete("/github/device")
        async def device_cancel(request: Request) -> JSONResponse:
            if not await _privileged(request.scope):
                return _forbidden()
            from backend.web.core import github_auth

            return JSONResponse(github_auth.cancel_device_flow())

        @router.post("/github/token")
        async def post_token(request: Request) -> JSONResponse:
            if not await _privileged(request.scope):
                return _forbidden()
            from backend.web.core import github_auth

            body = await _json(request)
            out = await asyncio.to_thread(
                github_auth.paste_token, str(body.get("token") or "")
            )
            if out.get("ok"):
                _changed()
            return JSONResponse(out, status_code=200 if out.get("ok") else 400)

        @router.post("/github/import-gh")
        async def import_gh(request: Request) -> JSONResponse:
            if not await _privileged(request.scope):
                return _forbidden()
            from backend.web.core import github_auth

            out = await asyncio.to_thread(github_auth.import_gh_token)
            if out.get("ok"):
                _changed()
            return JSONResponse(out, status_code=200 if out.get("ok") else 409)

        @router.websocket("/github/gh-login-terminal")
        async def gh_login_terminal(ws: WebSocket) -> None:
            """``gh auth login --web`` (then ``gh auth setup-git``) in a
            browser terminal — the command is fixed here, never sent by the
            client. Only for the person at this device: a sign-in lands
            credentials HERE."""
            from backend.web.core import github_auth, provider_login, pty_run

            await ws.accept()
            if not await _privileged(ws.scope):
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "error",
                            "message": "signing in is only allowed from this "
                            "computer or a signed-in device",
                        }
                    )
                )
                await ws.close(code=4403)
                return
            if not github_auth.gh_path():
                await ws.send_text(
                    json.dumps({"type": "error", "message": "gh isn't installed here"})
                )
                await ws.close(code=4500)
                return
            err = await asyncio.to_thread(
                provider_login.ensure_command_session,
                github_auth.GH_LOGIN_SESSION,
                github_auth.GH_LOGIN_COMMAND,
            )
            if err is not None:
                await ws.send_text(json.dumps({"type": "error", "message": err}))
                await ws.close(code=4500)
                return
            await pty_run.serve(ws, github_auth.GH_LOGIN_SESSION)

        @router.post("/github/gh-login-close")
        async def gh_login_close(request: Request) -> JSONResponse:
            if not await _privileged(request.scope):
                return _forbidden()
            from backend.web.core import github_auth, provider_login

            await asyncio.to_thread(
                provider_login.kill_session, github_auth.GH_LOGIN_SESSION
            )
            return JSONResponse({"ok": True})

        @router.post("/github/identity")
        async def post_identity(request: Request) -> JSONResponse:
            """Write the GLOBAL git ``user.name``/``user.email`` — the person
            clicked "Use these" in Setup; nothing writes it on its own."""
            if not await _privileged(request.scope):
                return _forbidden()
            from backend.web.core import github_auth

            body = await _json(request)
            out = await asyncio.to_thread(
                github_auth.set_git_identity,
                str(body.get("name") or ""),
                str(body.get("email") or ""),
            )
            if out.get("ok"):
                _changed()
            return JSONResponse(out, status_code=200 if out.get("ok") else 400)

        @router.post("/github/git-credential")
        async def post_git_credential(request: Request) -> JSONResponse:
            if not await _privileged(request.scope):
                return _forbidden()
            from backend.web.core import github_auth

            out = await asyncio.to_thread(github_auth.setup_git_credential)
            if out.get("ok"):
                _changed()
            return JSONResponse(out, status_code=200 if out.get("ok") else 409)

        @router.post("/github/push-check")
        async def push_check(request: Request) -> JSONResponse:
            """Can this computer push to the repo's origin? Body ``{repo}``
            (the remembered repo by default). Runs git against the network,
            so privileged — and a POST, so the Origin check applies — and
            only for a repo MindFlock already knows: the remembered one or a
            session's worktree, never an arbitrary path."""
            if not await _privileged(request.scope):
                return _forbidden()
            from backend.web.core import github_auth

            body = await _json(request)
            path = str(body.get("repo") or "") or github_auth.remembered_repo()
            path = os.path.realpath(os.path.expanduser(path)) if path else ""
            if path and path not in _known_repos():
                return JSONResponse(
                    {"error": "not a repo MindFlock knows: open it in a session first"},
                    status_code=400,
                )
            out = await asyncio.to_thread(github_auth.push_check, path)
            github_auth.remember_push_check(path, out)
            _changed()
            return JSONResponse(out)

        # ---------------------------------------------------------------- #
        # a new computer, and how ready each one is
        # ---------------------------------------------------------------- #
        @router.post("/fleet/bootstrap")
        async def post_bootstrap(request: Request) -> JSONResponse:
            """A fresh single-use code, as one install-and-join line for a
            computer that has nothing yet (pinned to this version)."""
            if not await _privileged(request.scope):
                return _forbidden()
            from backend.web.core import bootstrap, fleet

            body = await _json(request)
            inv = None
            want = str(body.get("code") or "")
            if want:  # the live invite the screen already shows
                inv = next((i for i in fleet.invites() if i["code"] == want), None)
            if inv is None:
                inv = await asyncio.to_thread(fleet.create_invite)
            return JSONResponse(bootstrap.lines(inv))

        @router.get("/fleet/readiness")
        async def get_readiness(
            request: Request, refresh: bool = False
        ) -> JSONResponse:
            if not await _privileged(request.scope):
                return _forbidden()
            from backend.web.core import readiness

            return JSONResponse(await readiness.gather(fresh=refresh))

        @router.get("/fleet/readiness/self")
        async def get_readiness_self(request: Request) -> JSONResponse:
            """This device's summary of itself, for another member (fleet
            key) — or the person here."""
            from backend.web.core import fleet, readiness

            if not (
                fleet.key_valid(_bearer(request)) or await _privileged(request.scope)
            ):
                try:
                    return JSONResponse(fleet.unauthorized_body(), status_code=401)
                except Exception:  # noqa: BLE001
                    return JSONResponse(
                        {"error": "not one of this device's devices"}, status_code=401
                    )
            return JSONResponse(await asyncio.to_thread(readiness.self_summary))

        return router
