"""The device tools: ``list_devices``, ``send_session`` and ``send_files``.

A mixin of :class:`backend.mcp.tools.Toolbox` (kept apart only for size).
All are thin calls of the server: ``GET /api/devices``, ``POST
/api/instances/{title}/send-to-device`` (push the branch — or carry it as a
git bundle when the device has no checkout — start a session on that device
ON the branch, hand it a brief: :mod:`backend.web.core.send_session`) and
``POST /api/instances/{title}/send-files`` (files into that device's inbox:
:mod:`backend.web.core.send_files`). Nothing here reads a file; the server
picks, filters, checksums and streams.

Who may send which session: the ship rule (:meth:`Policy.require_ship`) —
the caller's OWN session or one it manages (its descendants under scope
``children``), never a remote or pending row, never a team-run member, and
never past a ship lane the user set below ``push`` (a send pushes; a file
send follows the same rule — it carries the session's work away). It is the
stricter of the two candidates: "message/route any session" would let any
session push and carry off another's work, while every session may still
send ITSELF — the case the owner asks for ("send tripwire to my Mac").
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Dict, Optional, Tuple

from backend import client
from backend.mcp.protocol import ToolContext, ToolError

__all__ = ["DeviceTools", "D_LIST_DEVICES", "D_SEND_SESSION", "D_SEND_FILES"]

#: The server pushes (≤ 90 s), asks the device for its checkout and creates
#: the session there (≤ 60 s): the HTTP call gets room for all of it.
SEND_TIMEOUT_S = 200.0
#: A file send / bundle streams up to transfers.max_mb over the tailnet.
FILES_TIMEOUT_S = 1800.0

D_LIST_DEVICES = (
    "List the user's other MindFlock devices (Settings → Your devices) for "
    "send_session/send_files: {name, device, os, online, can_send, linked, "
    "sessions} plus this_device. name is what the user calls it, device its "
    "key (<device>::<title> rows); either works, case and punctuation "
    "ignored. can_send = linked, answering and taking sessions from here. "
    "spawn_session(device=) starts a new session on one; a <device>::<title> "
    "you started there you may message, wait for and close."
)
D_SEND_SESSION = (
    "Send a session to another of the user's devices, e.g. \"send tripwire to "
    "my Mac\". One call: pushes the session's branch (plain git push, never "
    "force), starts a session on that device ON the same branch in its "
    "checkout of the repo, and gives it a brief as its first prompt (what the "
    "session was for, its commits and changed files, its last reply, and your "
    "note). When that device has no checkout of the repo (or cannot reach "
    "its remote, or the repo has none) the branch goes instead as a "
    "standalone git bundle — a snapshot of where it started plus its "
    'commits — unpacked into a new repository there; paths=["tripwire"] '
    "sends only that folder (it becomes the repo root). via: auto (default) "
    "| git | bundle. Only git and that brief travel: no logins, no chat "
    "history, no secrets. title: yourself (default) or a session you "
    "manage. device: a name from list_devices. note: what to do there "
    '("install it as a service and start it"). Refused, with what to do, '
    "when the tree has uncommitted changes (commit first) or the device is "
    "asleep or not one of the user's. Returns {title: <device>::<title>, "
    "via, how, branch, repo_path, caveats}; read it with read_output/"
    "get_session (it is on another device: not your child)."
)
D_SEND_FILES = (
    "Send files or folders from your worktree to another of the user's "
    "devices over the device link. They land in a NEW folder there, "
    "<mindflock home>/inbox/<this device>/<id>/, SHA-256 checked, never "
    "run, executable bits removed (listed). paths: inside your worktree "
    "(folders recurse; git-ignored files — named ones too —, symlinks, "
    ".git, agent config/logins and secrets such as .env and keys are left "
    "out and listed; include_secrets only if the user allowed it in "
    "Settings; one ignored build artifact only by the user's own `mindflock "
    "send-files … --include-ignored`). "
    "Uncommitted files go too (listed). to_session: a session there to "
    "tell what arrived. note without to_session: starts a session there "
    "in that folder with the note as its task. Returns {path, files: "
    "[{path, sha256}], total_sha256, skipped, uncommitted, title?}. To "
    "carry a branch's work use send_session."
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
        "via": {
            "type": "string",
            "enum": ["auto", "git", "bundle"],
            "description": "auto (default): git when the device has the repo, "
            "else a bundle",
        },
        "paths": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
            "maxItems": 50,
            "description": "send only these repo paths, as a bundle",
        },
    },
    ("device",),
)
S_SEND_FILES = _obj(
    {
        "device": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": "the device to send them to (list_devices)",
        },
        "paths": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
            "minItems": 1,
            "maxItems": 100,
            "description": "files or folders in your worktree",
        },
        "note": {
            "type": "string",
            "maxLength": 1500,
            "description": "what they are for / what to do with them there",
        },
        "to_session": {
            "type": "string",
            "maxLength": 200,
            "description": "a session on that device to tell (its own title there)",
        },
        "include_secrets": {
            "type": "boolean",
            "description": "also send .env/keys/credentials (needs the user's "
            "Settings opt-in)",
        },
        "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": "session to send from (default: yourself)",
        },
    },
    ("device", "paths"),
)


_QUOTES = str.maketrans(
    {c: "'" for c in "\u2018\u2019\u201a\u201b\u2032\u02bc\uff07`\u00b4"}
)


def name_key(name: str) -> str:
    """How a device name compares (the server's ``remote.name_key``; this
    package stays stdlib-only): case, punctuation, spaces vs hyphens and
    curly quotes ignored."""
    import unicodedata

    text = unicodedata.normalize("NFKC", str(name or "")).translate(_QUOTES)
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def is_device_row(row: Optional[dict], title: str) -> bool:
    """A ``<device>::<title>`` row of one of the user's OWN devices (not a
    session another person shares over a peer link)."""
    return "::" in str(title or "") and not (
        isinstance(row, dict) and row.get("remote_peer") is True
    )


class DeviceTools:
    """The device handlers; mixed into ``Toolbox``."""

    # -- sessions THIS session started on another device ------------------- #
    def remote_control(self, flock, target: str, action: str, text: str = "") -> dict:
        """Act on ``<device>::<title>`` that the caller started there:
        ``status`` / ``message`` / ``close`` — through this server's
        ``/api/instances/{me}/remote-control``; the device checks that this
        session started it and refuses anything else."""
        me = flock.self_title
        if not me:
            raise ToolError(
                "acting on %r needs a MindFlock session identity: only the "
                "session that started it there may" % target
            )
        self.policy.require_write(flock, "acting on a session on another device")
        try:
            resp = self.api.post(
                self.api.inst_path(me, "/remote-control"),
                {"target": target, "action": action, "text": text},
                timeout=60.0,
            )
        except client.ApiError as err:
            code = err.payload.get("code") if isinstance(err.payload, dict) else ""
            if code == "not_origin":
                raise ToolError(
                    "%s: %s. Sessions on another device are read-only here "
                    "unless YOU started them (send_session, send_files, "
                    "spawn_session device=)." % (target, err.message)
                ) from None
            raise ToolError("%s(%s): %s" % (action, target, err.message)) from None
        return resp if isinstance(resp, dict) else {}

    def _spawn_on_device(self, args: dict, flock) -> dict:
        """``spawn_session(device=…)``: start a session on another of the
        user's devices, as THIS session's — it reports back here and closes
        itself there once done. Not a local child: address it as
        ``<device>::<title>``."""
        me = flock.self_title
        if not me:
            raise ToolError(
                "spawn_session(device=) needs a MindFlock session identity: the "
                "session there reports back to the one that started it"
            )
        device = str(args.get("device") or "").strip()
        repo = str(args.get("repo_path") or "").strip()
        if not repo:
            raise ToolError(
                "spawn_session(device=) needs repo_path: a folder ON %s (its "
                "repository there — that device's paths, not yours)" % device
            )
        try:
            listing = self.api.get("/api/devices", timeout=15.0)
        except client.ApiError as err:
            raise self._api_error(err, "spawn_session") from None
        want = name_key(device)
        hits = [
            d
            for d in (listing or {}).get("devices") or []
            if isinstance(d, dict)
            and want in {name_key(d.get("device") or ""), name_key(d.get("host") or "")}
        ]
        linked = [
            d
            for d in hits
            if (d.get("member") and d.get("same_fleet")) or d.get("has_token")
        ]
        hits = linked or hits
        if len(hits) != 1:
            names = ", ".join(
                "%s (%s)" % (d.get("host"), d.get("device")) for d in hits
            )
            raise ToolError(
                "no single device named %r%s — list_devices names them"
                % (device, ": did you mean " + names if names else "")
            )
        dev = hits[0]
        if not dev.get("connected"):
            raise ToolError(
                "%s is not answering or does not take sessions from here right "
                "now (list_devices: can_send)" % (dev.get("host") or dev.get("device"))
            )
        key = str(dev.get("device") or "")
        title = str(args.get("title") or "").strip() or "%s-remote" % me[:38]
        body: Dict[str, Any] = {
            "title": title,
            "repo_path": repo,
            "prompt": args["prompt"],
            "spawned": True,
            "origin": {"session": me, "by": "agent"},
        }
        if args.get("in_place"):
            body["in_place"] = True
        if args.get("program"):
            body["program"] = str(args["program"])
        try:
            resp = self.api.post(
                "/api/devices/%s/fwd/api/instances" % urllib.parse.quote(key, safe=""),
                body,
                timeout=SEND_TIMEOUT_S,
            )
        except client.ApiError as err:
            raise ToolError(
                "spawn_session on %s: %s" % (dev.get("host") or key, err.message)
            ) from None
        resp = resp if isinstance(resp, dict) else {}
        out = {
            "title": "%s::%s" % (key, resp.get("title") or title),
            "device": key,
            "device_name": dev.get("host") or key,
            "status": resp.get("status") or "",
            "remote": True,
            "hint": "not your child: wait_for_session / read_output / "
            "send_message / kill_session on this <device>::<title>; it "
            "reports back here and closes itself there once done",
        }
        if resp.get("queued"):
            out["queued"] = True
        return out

    def _require_sendable(self, flock, title: str, action: str) -> None:
        """The ship rule — but a session that is not live because it was
        just closed (an auto-closed worker) says so, with the command that
        reopens it, instead of "no session named"."""
        if flock.row(title) is None:
            try:
                closed = self.api.get("/api/recently-closed", timeout=10.0)
            except client.ApiError:
                closed = []
            hits = [
                e
                for e in (closed if isinstance(closed, list) else [])
                if isinstance(e, dict) and e.get("title") == title
            ]
            if hits:
                raise ToolError(
                    "%s was closed (it is in Recently closed) — ask the user to "
                    "reopen it (`mindflock reopen %s`, or Recently closed in "
                    "the sidebar), then %s again" % (title, title, action)
                )
        self.policy.require_ship(flock, title, action, depth="push")

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
                    "name": d.get("host") or d.get("device"),
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
        # Linked devices first: the ones a send can reach.
        out.sort(key=lambda d: (not d["linked"], str(d.get("name") or "").lower()))
        return {
            "this_device": me.get("device") or "",
            "this_device_name": me.get("host") or me.get("device") or "",
            "devices": out,
        }

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
        self._require_sendable(flock, title, "send_session")
        body = {
            "device": str(args["device"]).strip(),
            "note": args.get("note") or "",
            # Who sent it (the device shows "from <you> on <this device>",
            # and its reports come back to you).
            "from_session": flock.self_title or title,
            "started_by": "agent",
        }
        if args.get("via"):
            body["via"] = str(args["via"])
        if args.get("paths"):
            body["paths"] = [str(x) for x in args["paths"]]
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
            "via",
            "how",
            "branch",
            "pushed",
            "already_pushed",
            "commits",
            "repo_path",
            "bundle",
            "queued",
            "caveats",
        )
        out = {k: resp.get(k) for k in keep if k in resp}
        out["ok"] = True
        out["sent"] = title
        return out

    def send_files(self, args: dict, ctx: ToolContext) -> dict:
        flock = self.flock()
        title = str(args.get("title") or "").strip() or (flock.self_title or "")
        if not title:
            raise ToolError(
                "send_files needs title: you are not inside a MindFlock session"
            )
        if "::" in title:
            raise ToolError(
                "%r runs on another device; send its files from there" % title
            )
        self._require_sendable(flock, title, "send_files")
        body: Dict[str, Any] = {
            "device": str(args["device"]).strip(),
            "paths": [str(x) for x in args.get("paths") or []],
            "note": args.get("note") or "",
            "to_session": str(args.get("to_session") or "").strip(),
            "include_secrets": args.get("include_secrets") is True,
            "from_session": flock.self_title or title,
            "started_by": "agent",
        }
        try:
            resp = self.api.post(
                self.api.inst_path(title, "/send-files"),
                body,
                timeout=FILES_TIMEOUT_S,
            )
        except client.ApiError as err:
            if (
                err.status in (404, 405)
                and "instance not found" not in err.message
                and (not isinstance(err.payload, dict) or not err.payload.get("code"))
            ):
                raise ToolError(
                    "this MindFlock server cannot send files to other devices "
                    "yet; update it"
                ) from None
            raise ToolError("send_files(%s): %s" % (title, err.message)) from None
        resp = resp if isinstance(resp, dict) else {}
        keep = (
            "device",
            "path",
            "transfer_id",
            "files",
            "total_size",
            "total_sha256",
            "exec_stripped",
            "uncommitted",
            "skipped",
            "messaged",
            "title",
            "caveats",
        )
        out = {k: resp.get(k) for k in keep if k in resp}
        out["ok"] = True
        return out
