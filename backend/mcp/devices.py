"""The device tools: ``list_devices`` and ``send_session``.

A mixin of :class:`backend.mcp.tools.Toolbox` (kept apart only for size).
Both are thin calls of the server: ``GET /api/devices`` and ``POST
/api/instances/{title}/send-to-device``, which does the whole send (push the
branch, start a session on that device ON the branch, hand it a brief — see
:mod:`backend.web.core.send_session`). Only git and a text brief travel;
nothing here reads a file.

Who may send which session: the ship rule (:meth:`Policy.require_ship`) —
the caller's OWN session or one it manages (its descendants under scope
``children``), never a remote or pending row, never a team-run member, and
never past a ship lane the user set below ``push`` (a send pushes). It is the
stricter of the two candidates: "message/route any session" would let any
session push and carry off another's work, while every session may still
send ITSELF — the case the owner asks for ("send tripwire to my Mac").
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from backend import client
from backend.mcp.protocol import ToolContext, ToolError

__all__ = ["DeviceTools", "D_LIST_DEVICES", "D_SEND_SESSION"]

#: The server pushes (≤ 90 s), asks the device for its checkout and creates
#: the session there (≤ 60 s): the HTTP call gets room for all of it.
SEND_TIMEOUT_S = 200.0

D_LIST_DEVICES = (
    "List the user's other MindFlock devices (Settings → Your devices) for "
    "send_session: {device, os, online, can_send, linked, sessions} plus "
    "this_device. can_send = linked, answering and taking sessions from here."
)
D_SEND_SESSION = (
    "Send a session to another of the user's devices, e.g. \"send tripwire to "
    "my Mac\". One call: pushes the session's branch (plain git push, never "
    "force), starts a session on that device ON the same branch in its "
    "checkout of the repo, and gives it a brief as its first prompt (what the "
    "session was for, its commits and changed files, its last reply, and your "
    "note). Only git and that brief travel: no logins, no chat history. "
    "title: yourself (default) or a session you manage. device: a name from "
    'list_devices. note: what to do there ("install it as a launchd '
    'service"). Refused, with what to do, when the tree has uncommitted '
    "changes (commit first), the repo has no remote, the device is asleep or "
    "not one of the user's, or it has no checkout of the repo. Returns "
    "{title: <device>::<title>, branch, pushed, caveats}; read it with "
    "read_output/get_session (it is on another device: not your child)."
)


def _obj(props: dict, required: Tuple[str, ...] = ()) -> dict:
    out: Dict[str, Any] = {
        "type": "object",
        "properties": props,
        "additionalProperties": False,
    }
    if required:
        out["required"] = list(required)
    return out


S_LIST_DEVICES = _obj({})
S_SEND_SESSION = _obj(
    {
        "device": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": "the device to send it to (list_devices)",
        },
        "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": "session title (default: yourself)",
        },
        "note": {
            "type": "string",
            "maxLength": 1500,
            "description": "what the session should do there",
        },
    },
    ("device",),
)


class DeviceTools:
    """The two handlers; mixed into ``Toolbox``."""

    def list_devices(self, args: dict, ctx: ToolContext) -> dict:
        try:
            resp = self.api.get("/api/devices", timeout=15.0)
        except client.ApiError as err:
            raise self._api_error(err, "list_devices") from None
        resp = resp if isinstance(resp, dict) else {}
        out = []
        for d in resp.get("devices") or []:
            if not isinstance(d, dict):
                continue
            linked = bool(d.get("member") and d.get("same_fleet")) or bool(
                d.get("has_token")
            )
            out.append(
                {
                    "device": d.get("device"),
                    "os": d.get("os") or "",
                    "online": bool(d.get("reachable")),
                    "can_send": bool(linked and d.get("connected")),
                    "linked": linked,
                    "sessions": int(d.get("sessions") or 0),
                    **({"error": d["error"]} if d.get("error") else {}),
                }
            )
        me = resp.get("self") if isinstance(resp.get("self"), dict) else {}
        return {"this_device": me.get("device") or "", "devices": out}

    def send_session(self, args: dict, ctx: ToolContext) -> dict:
        flock = self.flock()
        title = str(args.get("title") or "").strip() or (flock.self_title or "")
        if not title:
            raise ToolError(
                "send_session needs title: you are not inside a MindFlock session"
            )
        if "::" in title:
            raise ToolError(
                "%r already runs on another device; send it from there" % title
            )
        self.policy.require_ship(flock, title, "send_session", depth="push")
        body = {"device": str(args["device"]).strip(), "note": args.get("note") or ""}
        try:
            resp = self.api.post(
                self.api.inst_path(title, "/send-to-device"),
                body,
                timeout=SEND_TIMEOUT_S,
            )
        except client.ApiError as err:
            if (
                err.status == 404
                and "instance not found" not in err.message
                and (not isinstance(err.payload, dict) or not err.payload.get("code"))
            ):
                raise ToolError(
                    "this MindFlock server cannot send sessions to other devices "
                    "yet; update it"
                ) from None
            raise ToolError("send_session(%s): %s" % (title, err.message)) from None
        resp = resp if isinstance(resp, dict) else {}
        keep = (
            "title",
            "device",
            "branch",
            "pushed",
            "already_pushed",
            "commits",
            "repo_path",
            "queued",
            "caveats",
        )
        out = {k: resp.get(k) for k in keep if k in resp}
        out["ok"] = True
        out["sent"] = title
        return out
