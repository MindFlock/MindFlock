"""``mindflock clean``: the worktree garbage collector from a terminal
(``backend/web/core/worktree_gc_driver.py``; docs/cli.md).

With no option it only LISTS (a dry run): every leftover worktree and session
clone, its size, whether it would be removed, and why the rest is kept.
``--collect`` removes the provably safe set; ``--remove ITEM`` removes one,
asking first (or ``--yes``) when it holds work, whose uncommitted changes are
saved to a kept folder before it goes; ``--mode`` / ``--after-days`` set the
background behaviour.

It prefers the running server (``--host`` / ``--port``): the server knows its
live sessions, and its own lock serializes removals. With no server (or
``--local``) it does the same work in-process from the files alone — the
sessions in ``state.json``, the recently-closed store, routes, runs and
queues — which is exactly what a stopped server would have used.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

from backend import client

#: How long the CLI waits for a server-side scan (du of a big disk is slow).
SCAN_WAIT_S = 900.0
#: Collection can remove dozens of folders: give it time.
COLLECT_TIMEOUT_S = 1800.0

_BADGE = {
    "safe": "remove",
    "recent": "recent",
    "holds_work": "has work",
    "in_use": "in use",
    "not_ours": "not ours",
    "missing": "missing",
    "unknown": "unknown",
}


def add_parser(sub, server_opts) -> None:
    p = sub.add_parser(
        "clean",
        parents=[server_opts],
        help="list leftover worktrees and what is safe to remove (--collect removes it)",
        description=(
            "The worktree garbage collector. With no option it only lists: "
            "every worktree and session clone MindFlock made (and other "
            "worktrees of the same repositories, which are never removed), how "
            "much space each takes, whether it would be removed and why the "
            "rest is kept. Safe = no session, route or run uses it, nothing "
            "uncommitted, every commit is in the base branch or on a remote "
            "(squash merges count), unused longer than cleanup.after_days. "
            "--collect removes exactly that set (a worktree with `git "
            "worktree remove`, its branch only when merged). --remove ITEM "
            "removes one item; one that holds work is removed only after you "
            "confirm, and its uncommitted changes are saved first as a patch "
            "in ~/.mindflock/kept-work/."
        ),
    )
    act = p.add_mutually_exclusive_group()
    act.add_argument(
        "--collect", action="store_true", help="remove every item marked 'remove' now"
    )
    act.add_argument(
        "--remove",
        metavar="ITEM",
        default=None,
        help="remove one item: its path, its name in the list, or its id",
    )
    p.add_argument(
        "--yes", "-y", action="store_true", help="with --remove: don't ask first"
    )
    p.add_argument(
        "--mode",
        choices=("auto", "ask", "off"),
        default=None,
        help="set the background behaviour: auto removes the safe set by itself, "
        "ask (default) says once when there is space to reclaim, off does neither",
    )
    p.add_argument(
        "--after-days",
        type=int,
        default=None,
        metavar="N",
        help="unused this many days before it counts as safe (default 14)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        dest="show_all",
        help="list every item, including ones in use and not MindFlock's",
    )
    p.add_argument(
        "--json", action="store_true", dest="as_json", help="print the report as JSON"
    )
    p.add_argument(
        "--local",
        action="store_true",
        help="don't use a running server; work from the files",
    )


# ------------------------------------------------------------- formatting


def human(n) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return ("%d%s" % (n, unit)) if unit == "B" else ("%.1f%s" % (n, unit))
        n /= 1024
    return "?"


def _ago(ts) -> str:
    if not ts:
        return "-"
    d = (time.time() - float(ts)) / 86400.0
    return "today" if d < 1 else "%dd ago" % int(d)


def render(rep: dict, show_all: bool = False) -> str:
    items = rep.get("items") or []
    root = rep.get("worktrees_root") or ""
    shown = [
        i for i in items if show_all or i.get("state") not in ("in_use", "not_ours")
    ]
    rows = []
    for i in shown:
        path = i.get("path") or ""
        name = (
            os.path.relpath(path, root)
            if root and path.startswith(root + os.sep)
            else path
        )
        why = (
            "merged"
            if i.get("safe") and i.get("merged")
            else ("pushed" if i.get("safe") else "; ".join(i.get("kept") or []))
        )
        rows.append(
            [
                _BADGE.get(i.get("state"), i.get("state") or "?"),
                human(i.get("size_bytes")),
                human(i.get("free_bytes")),
                _ago(i.get("last_used")),
                name,
                why,
            ]
        )
    from backend.cli import _render_table

    out = []
    if rows:
        out.append(
            _render_table(rows, ["STATE", "SIZE", "FREES", "USED", "ITEM", "WHY"])
        )
    s = rep.get("summary") or {}
    hidden = len(items) - len(shown)
    out.append("")
    out.append(
        "MindFlock made %d of these: %s on disk."
        % (s.get("count", 0), human(s.get("bytes", 0)))
    )
    if s.get("other_count"):
        out.append(
            "%d other worktree%s of the same repositories (%s) are listed, never removed."
            % (
                s["other_count"],
                "" if s["other_count"] == 1 else "s",
                human(s.get("other_bytes", 0)),
            )
        )
    if hidden:
        out.append(
            "%d in use or not MindFlock's are hidden; --all shows them." % hidden
        )
    if s.get("safe_count"):
        out.append(
            "%d can be removed safely (%s): mindflock clean --collect"
            % (s["safe_count"], human(s.get("safe_bytes")))
        )
    else:
        out.append("Nothing is safe to remove right now.")
    cfg = rep.get("settings") or {}
    out.append(
        "Background: %s, after %s days unused."
        % (cfg.get("mode", "ask"), cfg.get("after_days", 14))
    )
    return "\n".join(out)


# ---------------------------------------------------------------- routing


class _Server:
    def __init__(self, base: str) -> None:
        self.base = base

    def report(self) -> dict:
        rep = client.get(self.base, "/api/cleanup?refresh=1")
        deadline = time.monotonic() + SCAN_WAIT_S
        while (rep or {}).get("scanning") and time.monotonic() < deadline:
            time.sleep(2.0)
            rep = client.get(self.base, "/api/cleanup")
        return rep or {}

    def collect(self) -> dict:
        return client.post(
            self.base, "/api/cleanup/collect", {}, timeout=COLLECT_TIMEOUT_S
        )

    def remove(self, item_id: str, confirm: bool) -> dict:
        try:
            return client.post(
                self.base,
                "/api/cleanup/remove",
                {"id": item_id, "confirm": confirm},
                timeout=COLLECT_TIMEOUT_S,
            )
        except client.ApiError as err:
            if isinstance(err.payload, dict):
                return err.payload
            return {"ok": False, "error": err.message}

    def settings(self, mode, days) -> dict:
        return client.post(
            self.base, "/api/cleanup/settings", {"mode": mode, "after_days": days}
        )


class _Local:
    def __init__(self) -> None:
        from backend.web.core import worktree_gc_driver as drv

        self.drv = drv

    def report(self) -> dict:
        rep = self.drv.scan(None)
        rep["settings"] = self.drv.settings()
        return rep

    def collect(self) -> dict:
        return self.drv.collect_now(None)

    def remove(self, item_id: str, confirm: bool) -> dict:
        return self.drv.remove_item(None, item_id, confirm=confirm)

    def settings(self, mode, days) -> dict:
        return self.drv.set_settings(mode, days)


def _backend(args) -> object:
    if not args.local:
        try:
            return _Server(client.discover(args.host, args.port))
        except client.AuthRejected:
            raise
        except client.ServerNotFound:
            pass
    return _Local()


def _find(rep: dict, needle: str) -> Optional[dict]:
    items = rep.get("items") or []
    root = rep.get("worktrees_root") or ""
    full = os.path.realpath(os.path.expanduser(needle)) if needle else ""
    for i in items:
        p = i.get("path") or ""
        rel = os.path.relpath(p, root) if root and p.startswith(root + os.sep) else ""
        if needle in (i.get("id"), p, rel, i.get("name")) or full == p:
            return i
    matches = [i for i in items if os.path.basename(i.get("path") or "") == needle]
    return matches[0] if len(matches) == 1 else None


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(prompt + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_clean(args: argparse.Namespace) -> int:
    be = _backend(args)
    if args.mode is not None or args.after_days is not None:
        cfg = be.settings(args.mode, args.after_days)
        print(
            "cleanup: %s, after %s days unused"
            % (cfg.get("mode"), cfg.get("after_days"))
        )
        if not (args.collect or args.remove):
            return 0
    if args.collect:
        res = be.collect()
        for r in res.get("removed") or []:
            print("removed  %s  (%s)" % (r.get("path"), human(r.get("freed_bytes"))))
        for b in res.get("branches_deleted") or []:
            print("deleted merged branch %s" % b)
        for s in res.get("skipped") or []:
            print("kept     %s  — %s" % (s.get("path"), s.get("reason")))
        print(
            "%d removed, %s freed."
            % (len(res.get("removed") or []), human(res.get("freed_bytes")))
        )
        return 0
    rep = be.report()
    if args.remove:
        it = _find(rep, args.remove)
        if it is None:
            print(
                "error: no item matches %r (run `mindflock clean --all` to list them)"
                % args.remove,
                file=sys.stderr,
            )
            return 1
        confirm = bool(args.yes)
        if not it.get("safe") and not confirm:
            print("%s holds work or isn't MindFlock's:" % it.get("path"))
            for why in it.get("kept") or []:
                print("  - " + why)
            print(
                "Its uncommitted changes will be saved as a patch first; an unmerged branch is kept."
            )
            if not _confirm("Remove it anyway?"):
                print("Nothing removed.")
                return 1
            confirm = True
        res = be.remove(it["id"], confirm)
        if not res.get("ok"):
            print("error: %s" % (res.get("error") or "not removed"), file=sys.stderr)
            return 1
        print("removed %s" % it.get("path"))
        saved = res.get("saved") or {}
        if saved.get("patch"):
            print("uncommitted changes saved: %s" % saved["patch"])
        if saved.get("bundle"):
            print("unpushed commits saved: %s" % saved["bundle"])
        if saved.get("kept_branch"):
            print("its commits are kept on branch %s" % saved["kept_branch"])
        if res.get("branch_deleted"):
            print("deleted merged branch %s" % res["branch_deleted"])
        return 0
    if args.as_json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        print(render(rep, show_all=args.show_all))
    return 0
