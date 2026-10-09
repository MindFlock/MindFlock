"""``mindflock update``, ``mindflock restart`` and ``mindflock devices update``.

Thin clients over a running server, like the other session commands
(:mod:`backend.cli`): the server owns the install (``/api/update/*``, see
:mod:`backend.web.core.self_update`), restarts itself onto it
(:mod:`backend.web.core.update_watch`), and drives a fleet rollout
(``/api/fleet/update``, :mod:`backend.web.core.fleet_update`). What the CLI
adds is the waiting: it follows the install, then polls the public hello until
the server answers with the new build — the only proof the update took.

``update`` with no server running installs in place (the same detached
installer) and says to start the server afterwards. Headless machines (a rig
nobody sits at) are the point: before these commands, re-running install.sh
replaced the venv under a live server that kept running the old code.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Optional

from backend import client

#: Seconds between progress polls.
POLL_S = 2.0

#: How long a restart may take before the CLI gives up waiting for it.
RESTART_WAIT_S = 120.0


def _err(msg: str) -> None:
    print("error: %s" % msg, file=sys.stderr)


def _confirm(question: str) -> bool:
    try:
        answer = input(question + " [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return False
    return answer.strip().lower() in ("y", "yes")


def _hello(base: str) -> Optional[dict]:
    """The server's public hello, or None while it is down/restarting."""
    try:
        doc = client.get(base, "/api/remote/hello", timeout=3.0)
    except client.ClientError:
        return None
    return doc if isinstance(doc, dict) else None


def _build(hello: dict) -> str:
    commit = str(hello.get("commit") or "")
    return "v%s%s" % (
        hello.get("version") or "?",
        " (%s)" % commit[:7] if commit else "",
    )


def wait_for_build(
    base: str, version: str = "", commit: str = "", timeout: float = RESTART_WAIT_S
) -> Optional[dict]:
    """Poll the hello until the server answers — with ``commit`` (or, without
    one, ``version``) when given. Returns that hello, or None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hello = _hello(base)
        if hello:
            theirs = str(hello.get("commit") or "")
            if commit and theirs:
                if theirs == commit:
                    return hello
            elif not version or hello.get("version") == version:
                return hello
        time.sleep(POLL_S)
    return None


# --------------------------------------------------------------------------- #
# restart
# --------------------------------------------------------------------------- #
def cmd_restart(args: argparse.Namespace) -> int:
    """Re-exec the running server (picks up an engine installed under it)."""
    base = client.discover(args.host, args.port)
    client.post(base, "/api/server/restart", timeout=10.0)
    time.sleep(1.5)  # it answers once more before the re-exec lands
    hello = wait_for_build(base, timeout=60.0)
    if not hello:
        _err(
            "the server didn't come back within 60s — check its log "
            "(Settings → System logs, or ~/.mindflock/mindflock.log)"
        )
        return 1
    print("restarted — MindFlock %s" % _build(hello))
    return 0


# --------------------------------------------------------------------------- #
# update (this machine)
# --------------------------------------------------------------------------- #
def _follow(base: str, ref: str, commit: str) -> int:
    """Follow an install the server started until the server answers on it."""
    seen = "started"
    deadline = time.monotonic() + 35 * 60
    while time.monotonic() < deadline:
        time.sleep(POLL_S)
        try:
            st = client.get(base, "/api/update/state", timeout=10.0) or {}
        except client.ClientError:
            continue  # the server is mid-restart: keep waiting
        state = str(st.get("state") or "")
        if state in ("failed", "rolled_back"):
            for line in (st.get("log") or [])[-12:]:
                print("  " + str(line), file=sys.stderr)
            _err(
                str(st.get("error") or "")
                or "the install failed (exit %s) — see the lines above" % st.get("code")
            )
            return 1
        if state == "done" and seen != "done":
            seen = "done"
            print("installed — the server restarts onto it…")
            break
    if seen != "done":
        _err("timed out waiting for the install")
        return 1
    hello = wait_for_build(base, version=ref.lstrip("vV"), commit=commit)
    if not hello:
        _err(
            "installed, but the server didn't come back on %s within %ds — "
            "run `mindflock restart`, or check its log" % (ref, int(RESTART_WAIT_S))
        )
        return 1
    print("updated — MindFlock %s is running" % _build(hello))
    return 0


def _update_offline(args: argparse.Namespace) -> int:
    """No server running: install in place with the same detached installer."""
    import asyncio

    from backend.web.core import self_update

    current = self_update.installed_version()
    ref = str(args.ref or "").strip()
    if not ref:
        release = asyncio.run(self_update.latest_release(force=True))
        if not release:
            _err("couldn't reach GitHub to find the newest release")
            return 1
        print("This MindFlock: v%s · newest release: %s" % (current, release["tag"]))
        if args.check:
            return 0
        if not self_update.is_newer(release["version"], current):
            print("Already on the newest release.")
            return 0
        ref = release["tag"]
    elif args.check:
        print("This MindFlock: v%s" % current)
        return 0
    blocked = self_update.blocked_reason()
    if blocked:
        _err(blocked)
        return 1
    if not args.yes and not _confirm("Install %s?" % ref):
        print("not updated")
        return 1
    res = self_update.start_update(ref)
    if not res.get("ok"):
        _err(str(res.get("error") or "couldn't start the installer"))
        return 1
    print("installing %s (commit %s)…" % (ref, str(res.get("commit") or "")[:12]))
    deadline = time.monotonic() + self_update.INSTALL_TIMEOUT_S + 60
    while time.monotonic() < deadline:
        time.sleep(POLL_S)
        st = self_update.read_state()
        if st.get("state") == "done":
            print("installed %s — start it with: mindflock serve" % ref)
            return 0
        if st.get("state") in ("failed", "rolled_back"):
            for line in self_update.log_tail(12):
                print("  " + line, file=sys.stderr)
            _err(
                str(st.get("error") or "the install failed (exit %s)" % st.get("code"))
            )
            return 1
    _err("timed out waiting for the install")
    return 1


def cmd_update(args: argparse.Namespace) -> int:
    """``mindflock update [--ref vX] [--check] [--all-devices] [--yes]``."""
    if getattr(args, "all_devices", False):
        args.tag = args.ref
        return cmd_devices_update(args)
    try:
        base = client.discover(args.host, args.port)
    except client.ServerNotFound:
        return _update_offline(args)
    chk = client.get(base, "/api/update/check?refresh=1", timeout=20.0) or {}
    current, latest = str(chk.get("current") or "?"), str(chk.get("latest") or "")
    commit = str(chk.get("commit") or "")
    print(
        "This MindFlock: v%s%s" % (current, " (%s)" % commit[:7] if commit else "")
        + (" · newest release: v%s" % latest if latest else "")
    )
    if not chk.get("checked"):
        print("Couldn't reach GitHub to check for a newer release.")
    if chk.get("restart_pending"):
        print("An update is installed; the server restarts onto it shortly.")
    if args.check:
        return 0
    if chk.get("blocked"):
        _err(str(chk["blocked"]))
        return 1
    ref = str(args.ref or "").strip()
    if not ref:
        if not chk.get("available"):
            if chk.get("checked"):
                print("Already on the newest release.")
                return 0
            return 1
        ref = "v" + latest
    if not args.yes and not _confirm(
        "Update to %s and restart the server? (sessions keep running)" % ref
    ):
        print("not updated")
        return 1
    res = (
        client.post(
            base,
            "/api/update/start",
            {"ref": args.ref} if args.ref else {},
            timeout=120.0,
        )
        or {}
    )
    ref = str(res.get("ref") or ref)
    print("installing %s (commit %s)…" % (ref, str(res.get("commit") or "")[:12]))
    return _follow(base, ref, str(res.get("commit") or ""))


# --------------------------------------------------------------------------- #
# devices update (every device)
# --------------------------------------------------------------------------- #
_STEP_WORDS = {
    "queued": "waiting",
    "updating": "updating",
    "restarting": "restarting",
    "done": "updated",
    "current": "up to date",
    "skipped": "skipped",
    "failed": "FAILED",
    "not_started": "not started",
}


def _row_line(row: dict) -> str:
    word = _STEP_WORDS.get(str(row.get("step") or ""), str(row.get("step") or ""))
    detail = str(row.get("detail") or "")
    name = str(row.get("host") or row.get("key") or "?") + (
        " (this computer)" if row.get("self") else ""
    )
    return "  %s: %s%s" % (name, word, " — " + detail if detail else "")


def cmd_devices_update(args: argparse.Namespace) -> int:
    """Update every one of your devices, one at a time, this one last."""
    base = client.discover(args.host, args.port)
    st = client.get(base, "/api/fleet") or {}
    if not st.get("in_fleet"):
        _err(
            "this computer isn't grouped with your other devices — "
            "`mindflock update` updates just this one"
        )
        return 1
    members = [m for m in st.get("members") or [] if isinstance(m, dict)]
    for m in members:
        print(
            "  %s%s: v%s%s"
            % (
                m.get("host") or m.get("key"),
                " (this computer)" if m.get("self") else "",
                m.get("version") or "?",
                "" if m.get("self") or m.get("reachable") else " (offline)",
            )
        )
    tag = str(getattr(args, "tag", "") or "").strip()
    if not args.yes and not _confirm(
        "Update all %d devices to %s, one at a time (this one last)?"
        % (len(members), tag or "the newest release")
    ):
        print("not updated")
        return 1
    doc = client.post(
        base, "/api/fleet/update", {"tag": tag} if tag else {}, timeout=60.0
    )
    print("updating to %s…" % ((doc or {}).get("tag") or tag or "the newest release"))
    shown: dict = {}
    while True:
        try:
            doc = client.get(base, "/api/fleet/update", timeout=10.0) or {}
        except client.ClientError:
            time.sleep(POLL_S)  # this computer restarting at the very end
            continue
        for row in doc.get("members") or []:
            line = _row_line(row)
            if shown.get(row.get("key")) != line:
                shown[row.get("key")] = line
                print(line)
        if doc.get("state") != "running":
            break
        time.sleep(POLL_S)
    if doc.get("state") == "halted":
        _err("stopped: %s" % (doc.get("error") or "a device failed"))
        return 1
    print("done.")
    return 0
