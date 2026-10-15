"""Peer links (docs/peer-link.md): the ``/api/peer*`` routes.

Pairing with another person's MindFlock, one sandboxed shared folder per link.
The work lives in ``backend.peer.service``; these routes only translate. Every
one refuses a request proxied by another MindFlock device (X-MindFlock-Remote):
tailnet remote control must never manage peer links. No response carries a
key or secret, except the invite code in POST /api/peer/invites — the one
value the user must pass on.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend import log
from backend.web.core import auth as _auth
from backend.web.core import remote as _remote
from backend.web.routes import _server

router = APIRouter()


def _peer_service():
    from backend.peer import service as _svc

    return _svc.get_service()


def _service():
    """:func:`_peer_service`, read off the server namespace (tests patch it)."""
    return _server()._peer_service()


async def _peer_remote_refusal(request: Request) -> Optional[JSONResponse]:
    """Another MindFlock relaying never manages peer links here; and a change
    (an invite, a join, sharing a folder with someone, its perms) needs the
    owner (``auth.may_configure``) — not an anonymous tailnet caller of a
    gate-off device, who could otherwise share its folders with a peer of
    their own."""
    if _remote.from_remote(request):
        return JSONResponse(
            {"error": "peer links can only be managed on this device"},
            status_code=403,
        )
    if request.method != "GET" and not await _auth.may_configure(request.scope):
        return _auth.configure_refused()
    return None


async def _peer_call(request: Request, fn, *args, status: int = 200, **kwargs):
    refused = await _peer_remote_refusal(request)
    if refused is not None:
        return refused
    from backend.peer.service import PeerServiceError

    try:
        await _service().reconcile()
    except Exception as err:  # noqa: BLE001 — report, never 500 the status page
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("peer service reconcile failed: %v", err)
    try:
        out = fn(*args, **kwargs)
        if asyncio.iscoroutine(out):
            out = await out
    except PeerServiceError as err:
        return JSONResponse({"error": err.message}, status_code=err.status)
    except Exception as err:  # noqa: BLE001 — say what broke, never a bare 500
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("peer %s failed: %v", request.url.path, err)
        return JSONResponse(
            {"error": "%s: %s" % (type(err).__name__, str(err)[:400] or "failed")},
            status_code=500,
        )
    return JSONResponse(out if out is not None else {"ok": True}, status_code=status)


async def _peer_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — an empty or non-JSON body = {}
        return {}
    return body if isinstance(body, dict) else {}


@router.get("/api/peer")
async def peer_status(request: Request) -> JSONResponse:
    """Peer-link status: sandbox availability, this instance's identity
    fingerprint, the listen address, links (no keys) and active invites (id and
    expiry only — never the code)."""
    return await _peer_call(request, lambda: _service().status())


@router.post("/api/peer/enable")
async def peer_enable(request: Request) -> JSONResponse:
    """Turn peer links on without minting an invite (the "they're somewhere
    else" step: the doctor then offers cloudflared before the first invite)."""
    return await _peer_call(request, _service().enable)


@router.post("/api/peer/disable")
async def peer_disable(request: Request) -> JSONResponse:
    """Turn peer links off → ``{enabled: false, closed, links}``: the listener
    and connections stop and each shared session is closed (folder kept);
    every link and share stays listed for when they're turned on again."""
    return await _peer_call(request, _service().disable)


@router.post("/api/peer/invites")
async def peer_create_invite(request: Request) -> JSONResponse:
    """``{ttl_s?, advertise_host?, reach?, op_id?}`` → ``{invite_id, code,
    message, expires_in, host, port, direct, relay?, fallback?}``.
    ``reach`` is ``auto`` (default) | ``tunnel`` | ``direct``;
    ``advertise_host`` overrides the address a direct code carries (setting
    ``peer.advertise_host``, else Tailscale, else LAN); ``op_id`` names the
    ``peer.progress`` events. ``fallback: {reason, text}`` marks a direct
    invite made because the tunnel wouldn't start. The only response that
    ever contains an invite code; starts the listener."""
    body = await _peer_body(request)
    return await _peer_call(
        request,
        _service().create_invite,
        body.get("ttl_s"),
        str(body.get("advertise_host") or body.get("host") or ""),
        str(body.get("reach") or "auto"),
        str(body.get("op_id") or ""),
        status=201,
    )


@router.delete("/api/peer/invites/{invite_id}")
async def peer_revoke_invite(invite_id: str, request: Request) -> JSONResponse:
    return await _peer_call(request, _service().revoke_invite, invite_id)


@router.post("/api/peer/join")
async def peer_join(request: Request) -> JSONResponse:
    """``{code, op_id?}`` → the link (with its safety number to compare out
    of band) and ``reconnected`` — true when the code re-paired someone
    already linked (same link, share kept)."""
    body = await _peer_body(request)
    return await _peer_call(
        request,
        _service().join,
        str(body.get("code") or ""),
        str(body.get("op_id") or ""),
        status=201,
    )


@router.delete("/api/peer/links/{link_id}")
async def peer_unlink(link_id: str, request: Request) -> JSONResponse:
    """Unlink: close the shared session (its folder kept), forget the pinned
    key, close the connection. Only ``?delete_files=1`` deletes the folder
    (and the session); the peer's side closes theirs, never deletes it."""
    delete = request.query_params.get("delete_files", "") in ("1", "true", "yes")
    return await _peer_call(request, _service().unlink, link_id, delete_files=delete)


@router.post("/api/peer/links/{link_id}/address")
async def peer_set_address(link_id: str, request: Request) -> JSONResponse:
    """``{address}`` — point a link WE joined at the inviter's new address
    (``host:port`` or a relay ``wss://host/path``, e.g. after their quick
    tunnel restarted). The peer's key stays pinned, so this can only change
    where we dial, never whom we trust."""
    body = await _peer_body(request)
    return await _peer_call(
        request,
        _service().set_address,
        link_id,
        str(body.get("address") or ""),
    )


@router.post("/api/peer/links/{link_id}/verified")
async def peer_set_verified(link_id: str, request: Request) -> JSONResponse:
    """``{verified: bool}`` — "It matches": the safety number was compared."""
    body = await _peer_body(request)
    return await _peer_call(
        request, _service().set_verified, link_id, body.get("verified")
    )


@router.get("/api/peer/links/{link_id}/messages")
async def peer_messages(link_id: str, request: Request) -> JSONResponse:
    """The link's message log, both directions → ``{messages: [{id, dir,
    by, text, ts, delivered_to, read}], unread}``. Read-only."""
    try:
        limit = int(request.query_params.get("limit") or 100)
    except ValueError:
        limit = 100
    return await _peer_call(request, _service().messages, link_id, limit)


@router.post("/api/peer/links/{link_id}/messages/read")
async def peer_messages_read(link_id: str, request: Request) -> JSONResponse:
    return await _peer_call(request, _service().mark_messages_read, link_id)


@router.post("/api/peer/links/{link_id}/message")
async def peer_send_message(link_id: str, request: Request) -> JSONResponse:
    """``{text}`` — you, messaging the peer directly (no shared session
    needed on either side) → ``{msg_id, delivered}``."""
    body = await _peer_body(request)
    return await _peer_call(
        request, _service().human_message, link_id, body.get("text")
    )


@router.get("/api/peer/links/{link_id}/diff")
async def peer_their_changes(link_id: str, request: Request) -> JSONResponse:
    """The peer's shared-folder changes, read-only (their ``diff``; their
    permissions decide) → ``{stat, diff, truncated}``."""
    return await _peer_call(request, _service().their_changes, link_id)


@router.get("/api/peer/links/{link_id}/their-screen")
async def peer_their_screen(link_id: str, request: Request) -> JSONResponse:
    """What the peer's shared session shows right now → ``{text, cols, rows,
    agent}`` (``text`` keeps colours only; their "may see my changes"
    permission decides). Polled by the shared-session viewer."""
    return await _peer_call(request, _service().their_screen, link_id)


@router.get("/api/peer/links/{link_id}/their-files")
async def peer_their_files(link_id: str, request: Request) -> JSONResponse:
    """The files in the peer's shared folder → ``{files, truncated}`` (their
    "may read my files" permission decides)."""
    return await _peer_call(request, _service().their_files, link_id)


@router.get("/api/peer/links/{link_id}/their-file")
async def peer_their_file(link_id: str, request: Request) -> JSONResponse:
    """``?path=`` one file from the peer's shared folder → ``{path, size,
    encoding, content, truncated}``."""
    path = request.query_params.get("path", "")
    return await _peer_call(request, _service().their_file, link_id, path)


@router.post("/api/peer/links/{link_id}/perms")
async def peer_set_perms(link_id: str, request: Request) -> JSONResponse:
    """``{"messages"?, "diff"?, "read_file"?: bool}`` — what the PEER may do to
    us (``list_files`` follows ``read_file``)."""
    body = await _peer_body(request)
    perms = body.get("perms") if isinstance(body.get("perms"), dict) else body
    return await _peer_call(request, _service().set_perms, link_id, perms)


@router.post("/api/peer/links/{link_id}/share")
async def peer_share(link_id: str, request: Request) -> JSONResponse:
    """``{repo_path, branch?, program?, prompt?}`` → clone the repo into a fresh
    shared folder and start its sandboxed session (any agent CLI with a
    sandbox profile — see ``backend.peer.launch.allowed_providers``; 409
    without a working sandbox)."""
    body = await _peer_body(request)
    return await _peer_call(
        request,
        _service().share,
        link_id,
        str(body.get("repo_path") or ""),
        body.get("branch") or None,
        str(body.get("program") or ""),
        str(body.get("prompt") or ""),
        status=201,
    )


@router.post("/api/peer/links/{link_id}/share/reopen")
async def peer_reopen_share(link_id: str, request: Request) -> JSONResponse:
    """``{program?}`` → ``{link, session}``: start a new sandboxed session in
    the link's kept shared folder (its session was closed — peer links were
    off, or Close). 409 when nothing is shared, it's open, or it's gone."""
    body = await _peer_body(request)
    return await _peer_call(
        request,
        _service().reopen_share,
        link_id,
        str(body.get("program") or ""),
        status=201,
    )


@router.delete("/api/peer/links/{link_id}/share")
async def peer_unshare(link_id: str, request: Request) -> JSONResponse:
    """Stop sharing: close the shared session (folder kept, ``{folder}``);
    ``?delete_files=1`` deletes the session and the folder instead."""
    delete = request.query_params.get("delete_files", "") in ("1", "true", "yes")
    return await _peer_call(request, _service().unshare, link_id, delete_files=delete)


@router.post("/api/peer/links/{link_id}/export")
async def peer_export(link_id: str, request: Request) -> JSONResponse:
    """``{target_repo, branch_name}`` — checkpoint the share, then fetch it into
    your own repo as ``branch_name`` (must start with ``peer/``) →
    ``{branch, repo_path, commits, sha, target}``. 409 (one sentence) until
    the safety number is confirmed, or when the repo won't take it."""
    body = await _peer_body(request)
    return await _peer_call(
        request,
        _service().export,
        link_id,
        str(body.get("target_repo") or ""),
        str(body.get("branch_name") or ""),
    )


@router.post("/api/peer/links/{link_id}/mirror")
async def peer_mirror_enable(link_id: str, request: Request) -> JSONResponse:
    """``{repo_path, branch?}`` — mirror THEIR shared work into your repo as
    ``branch`` (default ``peer/<their name>``), now and on every checkpoint."""
    body = await _peer_body(request)
    return await _peer_call(
        request,
        _service().enable_mirror,
        link_id,
        str(body.get("repo_path") or ""),
        str(body.get("branch") or "") or None,
    )


@router.delete("/api/peer/links/{link_id}/mirror")
async def peer_mirror_disable(link_id: str, request: Request) -> JSONResponse:
    """Stop mirroring (the ``peer/`` branch stays in your repo)."""
    return await _peer_call(request, _service().disable_mirror, link_id)


# -- joint tasks (backend.peer.joint; docs/peer-link.md "Joint tasks") -------


def _joint():
    return _service().joint


@router.get("/api/peer/links/{link_id}/joint")
async def peer_joint(link_id: str, request: Request) -> JSONResponse:
    """The link's joint task → ``{joint: {…} | null}`` (this side's copy)."""
    return await _peer_call(request, lambda: _joint().get(link_id))


@router.post("/api/peer/links/{link_id}/joint")
async def peer_joint_propose(link_id: str, request: Request) -> JSONResponse:
    """``{brief, repo_path?, branch?, program?, contract_text?}`` — propose a
    joint task to the peer (my half's share starts when they accept). 409
    while one is open, before the safety number matches, or for an older
    peer."""
    body = await _peer_body(request)
    return await _peer_call(
        request,
        _joint().propose,
        link_id,
        body.get("brief"),
        str(body.get("repo_path") or ""),
        body.get("branch") or None,
        str(body.get("program") or ""),
        body.get("contract_text"),
        status=201,
    )


@router.post("/api/peer/links/{link_id}/joint/accept")
async def peer_joint_accept(link_id: str, request: Request) -> JSONResponse:
    """``{repo_path?, branch?, program?}`` — accept their proposal; starts my
    half's shared session → ``{joint, session?}``."""
    body = await _peer_body(request)
    return await _peer_call(
        request,
        _joint().accept,
        link_id,
        str(body.get("repo_path") or ""),
        body.get("branch") or None,
        str(body.get("program") or ""),
    )


@router.post("/api/peer/links/{link_id}/joint/cancel")
async def peer_joint_cancel(link_id: str, request: Request) -> JSONResponse:
    """``{reason?}`` — decline, cancel, or (``reason: "done"``) finish it.
    Shared folders keep running → ``{joint, synced}``."""
    body = await _peer_body(request)
    return await _peer_call(
        request, _joint().cancel, link_id, str(body.get("reason") or "")
    )


@router.put("/api/peer/links/{link_id}/joint/contract")
async def peer_joint_contract(link_id: str, request: Request) -> JSONResponse:
    """``{text}`` — a new revision of the contract (rev + 1, synced)."""
    body = await _peer_body(request)
    return await _peer_call(request, _joint().set_contract, link_id, body.get("text"))


@router.post("/api/peer/links/{link_id}/joint/proposals")
async def peer_joint_propose_edit(link_id: str, request: Request) -> JSONResponse:
    """``{text}`` — propose a contract change (the whole new text) →
    ``{proposal_id, synced, joint}``."""
    body = await _peer_body(request)
    return await _peer_call(
        request, _joint().propose_edit, link_id, body.get("text"), status=201
    )


@router.post("/api/peer/links/{link_id}/joint/proposals/{proposal_id}/accept")
async def peer_joint_accept_edit(
    link_id: str, proposal_id: str, request: Request
) -> JSONResponse:
    """Accept a proposal: its text becomes the contract (rev + 1)."""
    return await _peer_call(request, _joint().decide, link_id, proposal_id, True)


@router.post("/api/peer/links/{link_id}/joint/proposals/{proposal_id}/reject")
async def peer_joint_reject_edit(
    link_id: str, proposal_id: str, request: Request
) -> JSONResponse:
    return await _peer_call(request, _joint().decide, link_id, proposal_id, False)
