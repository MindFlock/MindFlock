"""Fleet addon: the HTTP surface of "Your devices" (Settings → Devices).

The state machines live in :mod:`backend.web.core.fleet`; this module is the
routes and who may call them. Three audiences, three rules:

* **The owner's browser** (every route that shows a code or lets someone in,
  joins, removes, leaves). Must be *privileged* —
  :func:`backend.web.core.auth.privileged`: a credential presented, or this
  machine itself, or a trusted tailnet account — and never relayed by another
  MindFlock (a member drives its peers' sessions; it must not drive their
  membership). Refused with 403 otherwise.

* **A device that wants in** (``redeem``, ``requests``, polling one request).
  Public — it holds no credential yet; that is the point. The auth middleware
  exempts exactly these three (method, path) shapes; each is validated hard,
  rate-limited per client IP, and answered only to a caller on the tailnet
  (a direct tailnet address, or one ``tailscale serve`` forwarded and this
  machine vouches for) or this machine itself — never to a LAN neighbour or
  an unvouched proxy hop, whose address can't say which device it is.

  Withdrawing a request (``requests/<id>/cancel``) is public the same way:
  it needs that request's secret.

* **Another member** (``roster``, ``rekey``, ``rotate-token``,
  ``update/apply``, ``update/state``, and the two that let a join be
  approved from wherever you are: ``pending`` — a member hands over the
  requests waiting on it — and ``member-approve`` — the answer given on
  another member, carrying the 6-digit code it showed) —
  authenticated with the fleet key, checked HERE against the key (not the
  device token), so a paired non-member can't read the roster. The auth
  middleware lets these through to the route whatever the bearer (see
  ``backend.web.core.auth._MEMBER_FLEET_ROUTES``), so a refusal is always
  this 401, naming this device's fleet id, key epoch and key fingerprint —
  not secret — so the caller can tell "I missed a key change" from "it did"
  from "we hold different keys". ``adopt`` is the odd
  one: the bearer must be this device's OWN access token, the proof a paired
  device presents when it adds us in one click.
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.web.core import auth as web_auth
from backend.web.core import fleet
from backend.web.core import fleet_update

from .base import Addon, AppContext

_FORBIDDEN = {"error": "open this on the device itself"}
_NOT_MEMBER = {"error": "not one of this device's devices"}
_REQ_ID = re.compile(r"^[a-f0-9]{16}$")


def _bad(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization") or ""
    return raw[7:].strip() if raw.lower().startswith("bearer ") else ""


def _client_ip(request: Request) -> str:
    """The caller's address: its tailnet IP when that can be believed
    (:func:`backend.web.core.tailnet_trust.peer_ip` — a direct tailnet
    connection, or one ``tailscale serve`` forwarded and this machine
    vouches for), else the socket peer as PeerCaptureMiddleware saw it.
    Unvouched serve-proxied callers all share 127.0.0.1 — one bucket, which
    errs toward limiting more, never less."""
    try:
        from backend.web.core import tailnet_trust as _tt

        ip = _tt.peer_ip(request.scope)
    except Exception:  # noqa: BLE001
        ip = None
    if ip:
        return str(ip)
    peer = request.scope.get("mf_peer") or request.scope.get("client") or ("", 0)
    return str(peer[0] or "")


_NOT_TAILNET = {"error": "join from one of your devices on the tailnet"}
#: The caller came through a local ``tailscale serve`` proxy this machine
#: can't vouch for (macOS/Windows): its address says nothing about which
#: device it is — the fix is on THIS device, not the joiner's.
_PROXIED = {
    "error": "this device is only reachable through tailscale serve, which hides "
    "who is asking — on it, open Settings → Devices and choose Make reachable"
}
#: How long a route waits on the members while it hands them the requests
#: waiting here (they only show a copy; a slow one just misses it).
FAN_OUT_TIMEOUT = 5.0


def _not_tailnet(request: Request) -> JSONResponse:
    try:
        from backend.web.core import tailnet_trust as _tt

        peer = request.scope.get("mf_peer") or request.scope.get("client") or ("", 0)
        if _tt.is_loopback(peer[0]) and _tt.has_forward_headers(request.scope):
            return JSONResponse(_PROXIED, status_code=403)
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse(_NOT_TAILNET, status_code=403)


async def _fan_out(exclude: Tuple[str, ...] = ()) -> None:
    """Hand the members the requests waiting here (bounded; never raises)."""
    try:
        await asyncio.wait_for(fleet.fan_out_requests(exclude), FAN_OUT_TIMEOUT)
    except Exception:  # noqa: BLE001 — a copy elsewhere is a convenience
        pass


def _join_caller_ok(request: Request) -> bool:
    """Whether a public join route may answer this caller: a tailnet address
    that can be believed (:func:`backend.web.core.tailnet_trust.peer_ip`), or
    this machine itself, unproxied. Never raises (fails closed)."""
    try:
        from backend.web.core import tailnet_trust as _tt

        ip = _tt.peer_ip(request.scope)
        if ip and _tt.is_tailnet_ip(ip):
            return True
        return bool(web_auth._from_this_machine(request.scope))
    except Exception:  # noqa: BLE001
        return False


def _not_member() -> JSONResponse:
    try:
        body = fleet.unauthorized_body()
    except Exception:  # noqa: BLE001
        body = dict(_NOT_MEMBER)
    return JSONResponse(body, status_code=401)


def _dns_arg(body: dict) -> str:
    dns = body.get("dns")
    return dns[:255] if isinstance(dns, str) else ""


async def _privileged(request: Request) -> bool:
    try:
        return bool(await web_auth.privileged(request.scope))
    except Exception:  # noqa: BLE001 — fail closed
        return False


async def _json(request: Request) -> Optional[dict]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return None
    return body if isinstance(body, dict) else None


def _device_arg(body: Optional[dict]) -> str:
    dev = str((body or {}).get("device") or "").strip().lower()
    return dev if fleet.DEVICE_RE.match(dev) else ""


def _via_arg(body: dict) -> str:
    """The member a request waits on (body ``via``), "" for one here."""
    via = str(body.get("via") or "").strip().lower()
    if not fleet.DEVICE_RE.match(via) or via == fleet._self_key():
        return ""
    return via


async def _admitted(out: dict) -> dict:
    """After an approve here: start what admitting needs on this side
    (fleet.after_admit — before the asker's next poll collects the bundle,
    since it pulls settings from here at once), and drop the request from
    the other members' copies."""
    runs = bool(out.pop("runs_automation", False))
    sync_error = await fleet.after_admit(runs, out["device"])
    await _fan_out((out["device"],))
    return {**out, "sync_error": sync_error}


async def _answer_relayed(
    via: str, rid: str, decision: str, body: dict
) -> JSONResponse:
    try:
        out = await fleet.answer_relayed(
            via, rid, decision, str(body.get("code") or "")[:16]
        )
    except KeyError:
        return _bad("that request is gone (expired or answered)", 404)
    except PermissionError as err:
        return _bad(str(err), 403)
    except RuntimeError as err:
        return _bad(str(err), 502)
    return JSONResponse(out)


def _join_response(join: dict) -> JSONResponse:
    return JSONResponse(join, status_code=400 if join.get("state") == "error" else 200)


class FleetAddon(Addon):
    id = "fleet"
    label = "Your devices"

    def __init__(self, ctx: Optional[AppContext] = None) -> None:
        super().__init__(ctx)
        self._router = self._build_router()

    @property
    def router(self) -> APIRouter:
        return self._router

    async def on_startup(self, ctx: AppContext) -> None:
        ctx.register_task(fleet.fleet_loop())

    def _build_router(self) -> APIRouter:  # noqa: C901 — one flat route table
        router = APIRouter(prefix="/api")

        # ---------------------------------------------------------------- #
        # the owner's browser
        # ---------------------------------------------------------------- #
        @router.get("/fleet")
        async def get_fleet(request: Request) -> JSONResponse:
            try:
                await fleet.refresh_exposure()  # for gate_warning
            except Exception:  # noqa: BLE001
                pass
            return JSONResponse(
                {
                    **fleet.status(privileged=await _privileged(request)),
                    # "Update all my devices": the last (or running) rollout.
                    "update": fleet_update.status(),
                }
            )

        @router.post("/fleet/update")
        async def post_fleet_update(request: Request) -> JSONResponse:
            """Update every member, one at a time, then this device — see
            :mod:`backend.web.core.fleet_update`. Body ``{"tag"?}`` (default:
            the newest release)."""
            if not await _privileged(request):
                return JSONResponse(_FORBIDDEN, status_code=403)
            body = await _json(request) or {}
            tag = body.get("tag")
            out, status = await fleet_update.start(
                tag[:64] if isinstance(tag, str) else ""
            )
            return JSONResponse(out, status_code=status)

        @router.get("/fleet/update")
        async def get_fleet_update(request: Request) -> JSONResponse:
            return JSONResponse(fleet_update.status())

        @router.post("/fleet/invite")
        async def post_invite(request: Request) -> JSONResponse:
            if not await _privileged(request):
                return JSONResponse(_FORBIDDEN, status_code=403)
            return JSONResponse(fleet.create_invite())

        @router.delete("/fleet/invite")
        async def delete_invite(request: Request) -> JSONResponse:
            if not await _privileged(request):
                return JSONResponse(_FORBIDDEN, status_code=403)
            fleet.cancel_invites()
            return JSONResponse({"ok": True})

        @router.post("/fleet/join")
        async def post_join(request: Request) -> JSONResponse:
            if not await _privileged(request):
                return JSONResponse(_FORBIDDEN, status_code=403)
            body = await _json(request) or {}
            device, code = _device_arg(body), str(body.get("code") or "")
            if body.get("text"):
                parsed_dev, code = fleet.parse_join_string(str(body["text"])[:512])
                device = parsed_dev or device
            if not device:
                return _bad("choose the device to join")
            if not code:
                return _bad("enter the code shown on the other device")
            try:
                return _join_response(await fleet.join_with_code(device, code))
            except ValueError as err:
                return _bad(str(err))

        @router.post("/fleet/request")
        async def post_request(request: Request) -> JSONResponse:
            if not await _privileged(request):
                return JSONResponse(_FORBIDDEN, status_code=403)
            device = _device_arg(await _json(request))
            if not device:
                return _bad("choose the device to join")
            try:
                return _join_response(await fleet.request_join(device))
            except ValueError as err:
                return _bad(str(err))

        @router.get("/fleet/request")
        async def get_request(request: Request) -> JSONResponse:
            if not await _privileged(request):
                return JSONResponse(_FORBIDDEN, status_code=403)
            return JSONResponse(fleet.join_status())

        @router.delete("/fleet/request")
        async def delete_request(request: Request) -> JSONResponse:
            if not await _privileged(request):
                return JSONResponse(_FORBIDDEN, status_code=403)
            return JSONResponse(fleet.cancel_join())

        @router.post("/fleet/requests/{rid}/approve")
        async def approve_request(rid: str, request: Request) -> JSONResponse:
            """Approve a join. ``via`` (body): the request waits on that
            member, not here — the answer goes there under the fleet key
            (fleet.answer_relayed); ``code``: the 6-digit code the person
            looked at, checked against the request's."""
            if not await _privileged(request):
                return JSONResponse(_FORBIDDEN, status_code=403)
            body = await _json(request) or {}
            via = _via_arg(body)
            if via:
                return await _answer_relayed(via, rid, "approve", body)
            req = fleet._REQUESTS.get(rid)
            if req is None or req.get("state") != "pending":
                # Before the code check: an answered request has no code to
                # match, and "doesn't match" would read like a forgery.
                return _bad("that request is gone (expired or answered)", 404)
            if body.get("code") and not fleet._code_matches(
                req.get("code", ""), str(body["code"])
            ):
                return _bad("that code doesn't match the request", 403)
            try:
                out = fleet.approve(rid)
            except KeyError:
                return _bad("that request is gone (expired or answered)", 404)
            return JSONResponse(await _admitted(out))

        @router.post("/fleet/requests/{rid}/deny")
        async def deny_request(rid: str, request: Request) -> JSONResponse:
            if not await _privileged(request):
                return JSONResponse(_FORBIDDEN, status_code=403)
            via = _via_arg(await _json(request) or {})
            if via:
                return await _answer_relayed(via, rid, "deny", {})
            try:
                out = fleet.deny(rid)
            except KeyError:
                return _bad("that request is gone (expired or answered)", 404)
            await _fan_out()
            return JSONResponse(out)

        @router.post("/fleet/add-paired")
        async def post_add_paired(request: Request) -> JSONResponse:
            if not await _privileged(request):
                return JSONResponse(_FORBIDDEN, status_code=403)
            device = _device_arg(await _json(request))
            if not device:
                return _bad("choose the device to add")
            try:
                return JSONResponse(await fleet.add_paired(device))
            except ValueError as err:
                return _bad(str(err))
            except RuntimeError as err:
                return _bad(str(err), 502)

        @router.post("/fleet/members/{key}/remove")
        async def remove_member(key: str, request: Request) -> JSONResponse:
            """Remove a member: the fleet key rotates and (``rotate_tokens``,
            default true) every member's own access token too — see
            :func:`backend.web.core.fleet.remove`."""
            if not await _privileged(request):
                return JSONResponse(_FORBIDDEN, status_code=403)
            body = await _json(request) or {}
            rotate = body.get("rotate_tokens", True) is not False
            try:
                out = await fleet.remove(key, rotate_tokens=rotate)
            except KeyError:
                return _bad("%s isn't one of your devices" % key, 404)
            resp = JSONResponse(out)
            if fleet._self_key() in (out.get("rotated") or []):
                # This device's token just changed: keep THIS browser signed in.
                resp = web_auth.set_auth_cookies(resp)
            return resp

        @router.post("/fleet/members/{key}/allow")
        async def allow_member(key: str, request: Request) -> JSONResponse:
            """Let a device this one removed back in HERE, once another
            member re-admitted it (status ``readmitted_elsewhere``) — see
            :func:`backend.web.core.fleet.allow`."""
            if not await _privileged(request):
                return JSONResponse(_FORBIDDEN, status_code=403)
            try:
                return JSONResponse(fleet.allow(key))
            except KeyError:
                return _bad("%s isn't a device removed here" % key, 404)

        @router.post("/fleet/leave")
        async def post_leave(request: Request) -> JSONResponse:
            if not await _privileged(request):
                return JSONResponse(_FORBIDDEN, status_code=403)
            return JSONResponse(await fleet.leave_fleet())

        # ---------------------------------------------------------------- #
        # public: a device that wants in (no credential yet)
        # ---------------------------------------------------------------- #
        @router.post("/fleet/redeem")
        async def post_redeem(request: Request) -> JSONResponse:
            if not _join_caller_ok(request):
                return _not_tailnet(request)
            if not fleet.allow_public(_client_ip(request)):
                return _bad("too many attempts — wait a minute", 429)
            body = await _json(request) or {}
            code = body.get("code")
            device = body.get("device")
            host = body.get("host") or device
            if (
                not isinstance(code, str)
                or not code
                or len(code) > 64
                or not isinstance(device, str)
                or not fleet.DEVICE_RE.match(device)
                or not isinstance(host, str)
                or len(host) > 255
            ):
                return _bad("bad request")
            try:
                out = fleet.redeem(
                    code, device, host, ip=_client_ip(request), dns=_dns_arg(body)
                )
            except fleet.TooManyAttempts as err:
                return _bad(str(err), 429)
            except PermissionError as err:
                return _bad(str(err), 403)
            except ValueError as err:
                return _bad(str(err))
            # Before answering: the joiner's very next call is a settings pull
            # from here, relayed (see fleet.after_admit).
            await fleet.after_admit(body.get("runs_automation") is True, device)
            return JSONResponse(out)

        @router.post("/fleet/requests")
        async def post_requests(request: Request) -> JSONResponse:
            if not _join_caller_ok(request):
                return _not_tailnet(request)
            if not fleet.allow_public(_client_ip(request)):
                return _bad("too many attempts — wait a minute", 429)
            body = await _json(request) or {}
            device = body.get("device")
            host = body.get("host") or device
            secret_hash = body.get("secret_hash")
            if (
                not isinstance(device, str)
                or not fleet.DEVICE_RE.match(device)
                or not isinstance(host, str)
                or len(host) > 255
                or not isinstance(secret_hash, str)
            ):
                return _bad("bad request")
            try:
                out = fleet.open_request(
                    device,
                    host,
                    secret_hash,
                    ip=_client_ip(request),
                    dns=_dns_arg(body),
                    runs_automation=body.get("runs_automation") is True,
                )
            except PermissionError as err:
                return _bad(str(err), 403)
            except ValueError as err:
                return _bad(str(err))
            # Approvable from any of your devices, not only this one.
            await _fan_out((device,))
            return JSONResponse(out)

        @router.get("/fleet/requests/{rid}")
        async def get_requests(rid: str, request: Request) -> JSONResponse:
            if not _join_caller_ok(request):
                return _not_tailnet(request)
            if not fleet.allow_public(_client_ip(request), "poll"):
                return _bad("too many attempts — wait a minute", 429)
            if not _REQ_ID.match(rid):
                return _bad("no such request", 404)
            try:
                return JSONResponse(
                    fleet.request_state(rid, request.query_params.get("secret", ""))
                )
            except KeyError:
                return _bad("no such request", 404)
            except PermissionError:
                return _bad("wrong secret", 403)

        @router.post("/fleet/requests/{rid}/cancel")
        async def cancel_requests(rid: str, request: Request) -> JSONResponse:
            """The asker withdraws its own pending request (Cancel, Ctrl-C):
            nobody can approve it afterwards. Needs the request's secret."""
            if not _join_caller_ok(request):
                return _not_tailnet(request)
            if not fleet.allow_public(_client_ip(request), "poll"):
                return _bad("too many attempts — wait a minute", 429)
            if not _REQ_ID.match(rid):
                return _bad("no such request", 404)
            body = await _json(request) or {}
            try:
                out = fleet.withdraw_request(rid, str(body.get("secret") or ""))
            except KeyError:
                return _bad("no such request", 404)
            except PermissionError:
                return _bad("wrong secret", 403)
            await _fan_out()
            return JSONResponse(out)

        # ---------------------------------------------------------------- #
        # member to member
        # ---------------------------------------------------------------- #
        @router.get("/fleet/roster")
        async def get_roster(request: Request) -> JSONResponse:
            if not fleet.key_valid(_bearer(request)):
                return _not_member()
            return JSONResponse(fleet.roster())

        @router.post("/fleet/roster")
        async def post_roster(request: Request) -> JSONResponse:
            if not fleet.key_valid(_bearer(request)):
                return _not_member()
            fleet.merge_roster(await _json(request) or {})
            # merge_roster may have just removed THIS device (left the fleet).
            return JSONResponse(fleet.roster())

        @router.post("/fleet/rekey")
        async def post_rekey(request: Request) -> JSONResponse:
            if not fleet.key_valid(_bearer(request)):
                return _not_member()
            return JSONResponse({"ok": fleet.apply_rekey(await _json(request) or {})})

        @router.post("/fleet/rotate-token")
        async def post_rotate_token(request: Request) -> JSONResponse:
            """A member removed another device and asks every member to
            replace its OWN access token (tokens the removed device held stop
            working). Only the CURRENT fleet key may ask."""
            if not fleet.key_valid(_bearer(request)):
                return _not_member()
            try:
                web_auth.rotate_token()
            except RuntimeError as err:  # pinned by MINDFLOCK_AUTH_TOKEN
                return JSONResponse({"ok": False, "error": str(err)})
            except Exception as err:  # noqa: BLE001 — settings store failure
                return JSONResponse(
                    {"ok": False, "error": "couldn't save a new token: %s" % err}
                )
            return JSONResponse({"ok": True})

        @router.post("/fleet/pending")
        async def post_pending(request: Request) -> JSONResponse:
            """A member hands over the join requests waiting on it (a
            snapshot), so the person can approve one from this device —
            fleet.hold_relayed. Fleet key only."""
            if not fleet.key_valid(_bearer(request)):
                return JSONResponse(_NOT_MEMBER, status_code=403)
            try:
                return JSONResponse(
                    {"ok": True, "held": fleet.hold_relayed(await _json(request) or {})}
                )
            except ValueError as err:
                return _bad(str(err))

        @router.post("/fleet/member-approve")
        async def post_member_approve(request: Request) -> JSONResponse:
            """The person answered a request waiting HERE on another member
            (its privileged approve/deny route sent it on). Fleet key only —
            never a relayed request that merely got past remote control, and
            never this device's own token — and the 6-digit code the other
            member showed must be this request's."""
            if not fleet.key_valid(_bearer(request)):
                return JSONResponse(_NOT_MEMBER, status_code=403)
            body = await _json(request) or {}
            rid = str(body.get("id") or "")
            if not _REQ_ID.match(rid):
                return _bad("no such request", 404)
            decision = str(body.get("decision") or "approve")
            try:
                out = fleet.decide_as_member(rid, str(body.get("code") or ""), decision)
            except KeyError:
                return _bad("that request is gone (expired or answered)", 404)
            except PermissionError as err:
                return _bad(str(err), 403)
            except ValueError as err:
                return _bad(str(err))
            if decision == "approve":
                return JSONResponse(await _admitted(out))
            await _fan_out()
            return JSONResponse(out)

        @router.post("/fleet/update/apply")
        async def post_update_apply(request: Request) -> JSONResponse:
            """Another member asks this device to update its engine — fleet
            key ONLY (not this device's token: a paired non-member must not
            be able to reinstall it), and only to a published release at or
            above what runs here (see :func:`fleet_update.apply`)."""
            if not fleet.key_valid(_bearer(request)):
                return _not_member()
            out, status = await fleet_update.apply(await _json(request) or {})
            return JSONResponse(out, status_code=status)

        @router.get("/fleet/update/state")
        async def get_update_state(request: Request) -> JSONResponse:
            if not fleet.key_valid(_bearer(request)):
                return _not_member()
            return JSONResponse(fleet_update.member_state())

        @router.post("/fleet/adopt")
        async def post_adopt(request: Request) -> JSONResponse:
            # This device's OWN token only — never the fleet key (a member
            # can't drag us into another group) and never tailnet trust.
            try:
                own = bool(web_auth.own_token_valid(_bearer(request)))
            except Exception:  # noqa: BLE001 — fail closed
                own = False
            if not own:
                return _bad("needs this device's own access token", 401)
            try:
                return JSONResponse(
                    await fleet.adopt_from_peer(await _json(request) or {}, own)
                )
            except ValueError as err:
                return _bad(str(err), 409 if "leave that group" in str(err) else 400)

        return router
