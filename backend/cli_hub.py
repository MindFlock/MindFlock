"""``mindflock hub`` — the hub of shared files, pictures and snippets
from a terminal (docs/cli.md "mindflock hub"; the server's ``/api/hub``,
:mod:`backend.web.core.hub`).

    mindflock hub ls                     the items, newest first
    mindflock hub add FILE… [--note N]   add files (or --text "…", or - for stdin)
    mindflock hub get REF [--text]       its path (or its text)
    mindflock hub cp REF [DEST]          copy it here (never over a file
                                          unless --replace); --to TITLE copies
                                          it into that session's folder
    mindflock hub rm REF                 remove it (Undo in the app for 10 min)
    mindflock hub pin REF [--off]        keep it past the expiry

REF is the item's reference, ``hub:k7m2`` (or just ``k7m2``).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
import urllib.parse
from typing import Optional

from backend import client


def add_parser(sub, server_opts) -> None:
    hub = sub.add_parser(
        "hub",
        parents=[server_opts],
        help="the hub: files, pictures and snippets every session can use",
        description="The hub: one shared place for files, pictures and "
        "snippets every session on this device can use. Each item has a "
        "reference like hub:k7m2 — type it in any prompt you send through "
        "MindFlock and the agent gets the item's path. Unpinned items are "
        "removed hub.keep_days (14) after they were added or last used.",
    )
    hs = hub.add_subparsers(dest="hub_command")
    ls_ = hs.add_parser("ls", help="list the items, newest first")
    ls_.add_argument("--json", action="store_true", help="print JSON")
    add_ = hs.add_parser("add", help="add files, or text")
    add_.add_argument(
        "files",
        nargs="*",
        metavar="FILE",
        help="files to add (- reads text from stdin)",
    )
    add_.add_argument("--text", default=None, help="add this text as a snippet")
    add_.add_argument("--name", default="", help="the item's name")
    add_.add_argument("--note", default="", help="a short note")
    add_.add_argument("--pin", action="store_true", help="keep it past the expiry")
    add_.add_argument(
        "--secret-ok",
        action="store_true",
        help="add a file that looks like a secret (.env, a key) anyway — every "
        "session can read the hub",
    )
    get_ = hs.add_parser("get", help="print an item's path (or its text)")
    get_.add_argument("ref", metavar="REF")
    get_.add_argument("--text", action="store_true", help="print its text instead")
    cp_ = hs.add_parser("cp", help="copy an item here, or into a session's folder")
    cp_.add_argument("ref", metavar="REF")
    cp_.add_argument("dest", nargs="?", default="", metavar="DEST")
    cp_.add_argument(
        "--to",
        dest="session",
        default="",
        metavar="TITLE",
        help="copy it into this session's folder (DEST relative to it; "
        "default hub/<name>)",
    )
    cp_.add_argument("--replace", action="store_true", help="replace an existing file")
    rm_ = hs.add_parser("rm", help="remove an item")
    rm_.add_argument("refs", nargs="+", metavar="REF")
    pin_ = hs.add_parser("pin", help="pin an item (kept past the expiry)")
    pin_.add_argument("ref", metavar="REF")
    pin_.add_argument("--off", action="store_true", help="unpin it")


def _id(ref: str) -> str:
    v = ref.strip()
    if v.lower().startswith("hub:"):
        v = v[4:]
    return urllib.parse.quote(v.lower(), safe="")


def _human(n: int) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return ("%d %s" % (size, unit)) if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024
    return "%d B" % n


#: The kinds as people read them (the API's image/text/file).
KIND_WORD = {"image": "picture", "text": "snippet", "file": "file"}


def _who(rec: dict) -> str:
    by = rec.get("by")
    if by == "session":
        return "session %s" % rec.get("by_name", "")
    if by == "device":
        return "device %s" % rec.get("by_name", "")
    return {"phone": "your phone"}.get(by or "", "you")


def _when(ts) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%b %d %H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


def cmd_hub(args: argparse.Namespace) -> int:
    base = client.discover(args.host, args.port)
    cmd = getattr(args, "hub_command", None) or "ls"
    if cmd == "ls":
        res = client.get(base, "/api/hub") or {}
        items = res.get("items") or []
        if getattr(args, "json", False):
            print(json.dumps(res, indent=2))
            return 0
        if not items:
            print(
                "The hub is empty. Add something: mindflock hub add FILE "
                "(or drop/paste it on the Hub panel in the app)."
            )
            return 0
        for r in items:
            print(
                "%-10s %-7s %8s  %-36s %s, %s%s"
                % (
                    r["ref"],
                    KIND_WORD.get(r.get("kind", ""), "file"),
                    _human(r.get("size", 0)),
                    r.get("name", "")[:36],
                    _who(r),
                    _when(r.get("added")),
                    " · pinned" if r.get("pinned") else "",
                )
            )
        print(
            "%d items · %s of %s used · type a reference (like %s) in any prompt"
            % (
                res.get("count", len(items)),
                _human(res.get("used", 0)),
                _human(res.get("quota", 0)),
                items[0]["ref"],
            )
        )
        return 0
    if cmd == "add":
        return _add(base, args)
    if cmd == "get":
        rec = client.get(base, "/api/hub/items/%s" % _id(args.ref)) or {}
        if args.text:
            body = client.get(base, "/api/hub/items/%s/text" % _id(args.ref)) or {}
            sys.stdout.write(body.get("text", ""))
            if body.get("truncated"):
                print(
                    "\n[… truncated — the whole file is at %s]" % rec.get("path"),
                    file=sys.stderr,
                )
            return 0
        print(rec.get("path", ""))
        return 0
    if cmd == "cp":
        return _cp(base, args)
    if cmd == "rm":
        rc = 0
        for ref in args.refs:
            try:
                client.delete(base, "/api/hub/items/%s" % _id(ref))
                print("removed %s (Undo in the app's Hub panel for 10 minutes)" % ref)
            except client.ApiError as err:
                print("error: %s" % err.message, file=sys.stderr)
                rc = 1
        return rc
    if cmd == "pin":
        client._request(  # noqa: SLF001 — PATCH with a JSON body
            base + "/api/hub/items/%s" % _id(args.ref),
            json.dumps({"pinned": not args.off}).encode("utf-8"),
            client.REQUEST_TIMEOUT_S,
            method="PATCH",
        )
        print("%s %s" % ("unpinned" if args.off else "pinned", args.ref))
        return 0
    print(
        "error: unknown hub command %r (ls, add, get, cp, rm, pin)" % cmd,
        file=sys.stderr,
    )
    return 2


def _add(base: str, args: argparse.Namespace) -> int:
    files = list(args.files or [])
    if args.text is None and not files:
        print(
            'error: name a FILE to add, or --text "…" (- reads stdin)', file=sys.stderr
        )
        return 2
    rc = 0
    texts = []
    if args.text is not None:
        texts.append(args.text)
    if "-" in files:
        files.remove("-")
        texts.append(sys.stdin.read())
    for t in texts:
        rec = (
            client.post(
                base, "/api/hub/text", {"text": t, "name": args.name, "note": args.note}
            )
            or {}
        )
        _pin_if(base, args, rec)
        print("added %s  %s" % (rec.get("ref"), rec.get("name")))
    for f in files:
        try:
            st = os.stat(f)
        except OSError as err:
            print("error: %s: %s" % (f, err.strerror), file=sys.stderr)
            rc = 1
            continue
        if not os.path.isfile(f):
            print("error: %s is not a file (zip a folder first)" % f, file=sys.stderr)
            rc = 1
            continue
        name = args.name if (args.name and len(files) == 1) else os.path.basename(f)
        q = urllib.parse.urlencode(
            {"name": name, "note": args.note, "secret_ok": 1 if args.secret_ok else 0}
        )
        with open(f, "rb") as fh:
            data = fh.read()
        try:
            rec = (
                client._request(  # noqa: SLF001 — a raw-body upload
                    base + "/api/hub/items?" + q, data, max(60.0, st.st_size / 2e6)
                )
                or {}
            )
        except client.ApiError as err:
            msg = err.message
            if isinstance(err.payload, dict) and err.payload.get("can_confirm"):
                msg += " (add it anyway with --secret-ok)"
            print("error: %s: %s" % (f, msg), file=sys.stderr)
            rc = 1
            continue
        _pin_if(base, args, rec)
        print(
            "added %s  %s (%s)"
            % (rec.get("ref"), rec.get("name"), _human(rec.get("size", 0)))
        )
    return rc


def _pin_if(base: str, args: argparse.Namespace, rec: dict) -> None:
    if args.pin and rec.get("id"):
        client._request(  # noqa: SLF001
            base + "/api/hub/items/%s" % _id(rec["id"]),
            json.dumps({"pinned": True}).encode("utf-8"),
            client.REQUEST_TIMEOUT_S,
            method="PATCH",
        )


def _cp(base: str, args: argparse.Namespace) -> int:
    if args.session:
        body = {"id": args.ref, "replace": bool(args.replace)}
        if args.dest:
            body["path"] = args.dest
        res = (
            client.post(
                base,
                "/api/instances/%s/hub/take"
                % urllib.parse.quote(args.session, safe=""),
                body,
                timeout=300.0,
            )
            or {}
        )
        print(
            "%s %s into %s: %s"
            % (
                "replaced" if res.get("replaced") else "copied",
                args.ref,
                args.session,
                res.get("path"),
            )
        )
        return 0
    rec = client.get(base, "/api/hub/items/%s" % _id(args.ref)) or {}
    dest = args.dest or rec.get("name") or "item"
    if os.path.isdir(dest):
        dest = os.path.join(dest, rec.get("name") or "item")
    if os.path.lexists(dest) and not args.replace:
        print(
            "error: %s already exists — pick another DEST, or --replace" % dest,
            file=sys.stderr,
        )
        return 1
    src = rec.get("path") or ""
    fd: Optional[int] = None
    try:
        fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as fin:
            fd = None
            with open(dest, "wb") as fout:
                shutil.copyfileobj(fin, fout)
    except OSError as err:
        print("error: could not copy %s: %s" % (args.ref, err), file=sys.stderr)
        return 1
    finally:
        if fd is not None:
            os.close(fd)
    print("copied %s to %s" % (args.ref, dest))
    return 0
