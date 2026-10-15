"""``mindflock update``, ``mindflock restart`` and ``mindflock devices update``.

Thin clients over a running server, like the other session commands
(:mod:`backend.cli`): the server owns the install (``/api/update/*``, see
:mod:`backend.web.core.self_update`), restarts itself onto it
(:mod:`backend.web.core.update_watch`), and drives a fleet rollout
(``/api/fleet/update``, :mod:`backend.web.core.fleet_update`). What the CLI
adds is the waiting: it follows the install, then polls the public hello until
the server answers with the new build — the only proof the update took.

``update`` is the exception: it runs the install HERE, in the foreground,
with the method ``install.json`` records (uv tool, pipx — or, for a
development checkout, it only prints the git commands), then restarts a
running server onto the result. Headless machines (a rig nobody sits at) are
the point: before these commands, re-running install.sh replaced the venv
under a live server that kept running the old code.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import shlex
import subprocess
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


_SEMVER = re.compile(r"^\d+\.\d+")


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
                # `--ref <short sha>`: either side may be the abbreviation.
                if theirs.startswith(commit) or commit.startswith(theirs):
                    return hello
            elif (
                not version
                or not _SEMVER.match(version)
                or hello.get("version") == version
            ):
                # A branch or sha ref has no version to match: any build that
                # answers after the restart is the new one.
                return hello
        time.sleep(POLL_S)
    return None


# --------------------------------------------------------------------------- #
# restart
# --------------------------------------------------------------------------- #
def cmd_restart(args: argparse.Namespace) -> int:
    """Re-exec the running server (picks up an engine installed under it),
    in the mode it runs in — a server started with ``mindflock serve
    tailscale`` stays on the tailnet."""
    base = client.discover(args.host, args.port)
    client.post(base, "/api/server/restart", {"keep_mode": True}, timeout=10.0)
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
#: How install.json's / install_kind's answer reads on a terminal.
_METHOD_WORDS = {
    "uv-tool": "uv tool",
    "pipx": "pipx",
    "editable": "a development checkout (editable)",
    "other": "an installer MindFlock doesn't recognise",
}

_SHA = re.compile(r"^[0-9a-f]{7,64}$")


def _run_installer(argv: list) -> int:
    """Run the install command in the foreground, its output on this
    terminal. Returns the exit code (1 when it couldn't start)."""
    print("$ " + " ".join(shlex.quote(a) for a in argv))
    try:
        return int(subprocess.run(argv, check=False).returncode)  # noqa: S603
    except (OSError, subprocess.SubprocessError) as err:
        _err("couldn't run %s: %s" % (argv[0], err))
        return 1


def _carry_drivers(method: str) -> None:
    """Put the Database explorer's recorded drivers back after a pipx rebuild
    (uv carries them as ``--with`` in the install line itself). A failure only
    warns: the explorer reinstalls a missing driver when the server starts."""
    from backend.web.core import self_update

    for argv in self_update.driver_carry_commands(method):
        if _run_installer(argv) != 0:
            _err(
                "couldn't carry the database drivers over — MindFlock "
                "reinstalls them when the server starts"
            )


def _rollback(previous: Optional[dict]) -> str:
    """The boot-failure safety net the server-driven updater had: put the
    engine ``install.json`` described BEFORE this update back and restart onto
    it. Returns a sentence for the user ("" when there was nothing to go back
    to or the rollback itself failed)."""
    if not previous:
        return ""
    from backend.web.core import self_update

    method = str(previous.get("method") or "")
    target = str(previous.get("sha") or previous.get("ref") or "")
    if method not in ("uv-tool", "pipx") or not target:
        return ""
    argv = self_update.install_command(method, target)
    if not argv:
        return ""
    print("rolling back to %s…" % (previous.get("ref") or target))
    if _run_installer(argv) != 0:
        return ""
    _carry_drivers(method)
    self_update.write_install_json(
        method=method,
        ref=previous.get("ref"),
        sha=previous.get("sha") or None,
        at=int(time.time()),
    )
    return "rolled the engine back to %s (run `mindflock restart` to boot it)" % (
        previous.get("ref") or target
    )


def _restart_running_server(
    args: argparse.Namespace, ref: str, commit: str, previous: Optional[dict] = None
) -> int:
    """After a successful install: if a server is running, restart it onto
    the new engine (``POST /api/server/restart``, same mode) and print the
    version it reports once back. With none, say how to start it."""
    try:
        base = client.discover(args.host, args.port)
    except client.ServerNotFound:
        print("installed %s — start it with: mindflock serve" % ref)
        return 0
    try:
        client.post(base, "/api/server/restart", {"keep_mode": True}, timeout=10.0)
    except client.ClientError as err:
        _err(
            "installed %s, but the running server couldn't be asked to restart "
            "(%s) — run `mindflock restart`" % (ref, err)
        )
        return 1
    print("restarting the server onto it…")
    time.sleep(1.5)  # it answers once more before the re-exec lands
    hello = wait_for_build(base, version=ref.lstrip("vV"), commit=commit)
    if not hello:
        rolled = _rollback(previous)
        _err(
            "installed %s, but the server did not come back within %ds — "
            "%s"
            % (
                ref,
                int(RESTART_WAIT_S),
                rolled or "run `mindflock restart`, or check its log",
            )
        )
        return 1
    print("updated — MindFlock %s is running" % _build(hello))
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    """``mindflock update [--check] [--ref <tag|sha>] [--yes] [--all-devices]``.

    One update path for every install method. ``install.json`` (what
    ``install.sh`` wrote: ``~/.mindflock/install.json``) says how this
    engine was installed, with the running package's own location as the
    fallback (:func:`backend.web.core.self_update.install_kind`):

    * ``uv-tool`` — the pinned ``uv tool install --force`` the server's own
      updater runs (the tag resolved to its commit first);
    * ``pipx`` — ``pipx install --force "mindflock[web] @ git+<repo>@<tag>"``;
    * ``editable`` — prints the git commands and changes nothing (exit 0).

    Database explorer drivers recorded in ``~/.mindflock/dbclient/drivers.json``
    ride along: one ``--with`` each on the uv line, ``pipx inject`` after pipx.

    ``--check`` prints the installed and newest versions and exits 0 when
    current, 1 when behind (2 when GitHub couldn't be reached). After a good
    install a running server is restarted onto the new engine and the
    version it then reports is printed.
    """
    if getattr(args, "all_devices", False):
        args.tag = args.ref
        return cmd_devices_update(args)
    from backend.web.core import self_update

    current = self_update.installed_version()
    commit_now = self_update.installed_commit()
    kind = self_update.install_kind()
    recorded = self_update.read_install_json()
    source = (
        "install.json"
        if kind != "editable" and recorded.get("method") == kind
        else "detected"
    )
    latest_tag = asyncio.run(self_update.latest_release_tag(force=True))
    latest = latest_tag.lstrip("vV")
    print(
        "This MindFlock: v%s%s · installed with %s (%s)%s"
        % (
            current or "?",
            " (%s)" % commit_now[:7] if commit_now else "",
            _METHOD_WORDS.get(kind, kind),
            source,
            " · newest release: v%s" % latest if latest else "",
        )
    )
    behind = bool(latest) and self_update.is_newer(latest, current)
    if not latest_tag:
        print("Couldn't reach GitHub to check for a newer release.")
    if args.check:
        if not latest_tag:
            return 2
        print(
            "Already on the newest release."
            if not behind
            else "An update is available."
        )
        return 0 if not behind else 1
    ref = str(args.ref or "").strip() or latest_tag
    if kind == "editable":
        print(
            "This is a development checkout, so the update comes from git. "
            "Run these yourself:"
        )
        for line in self_update.editable_update_commands(ref):
            print("  " + line)
        return 0
    blocked = self_update.blocked_reason()
    if blocked:
        _err(blocked)
        return 1
    if not args.ref:
        if not latest_tag:
            return 1
        if not behind:
            print("Already on the newest release.")
            return 0
    if not args.yes and not _confirm(
        "Install %s with %s%s?"
        % (
            ref,
            _METHOD_WORDS.get(kind, kind),
            " and restart the server (sessions keep running)",
        )
    ):
        print("not updated")
        return 1
    # uv pins the COMMIT (a tag can move under an install); pipx takes the tag
    # (its spec records what was asked for, which is what `pipx list` shows).
    commit = self_update._resolve_commit(ref) if kind == "uv-tool" else ""
    if kind == "uv-tool" and not commit:
        if _SHA.match(ref):
            commit = ref
        else:
            _err("could not resolve %s on %s" % (ref, self_update.INSTALL_REPO))
            return 1
    argv = self_update.install_command(kind, commit or ref)
    if not argv:
        _err(self_update.blocked_reason() or "no installer for %s" % kind)
        return 1
    print("installing %s…" % ref)
    code = _run_installer(argv)
    if code != 0:
        _err("the install failed (exit %d) — see the lines above" % code)
        return 1
    _carry_drivers(kind)
    self_update.write_install_json(
        method=kind, ref=ref, sha=commit or None, at=int(time.time())
    )
    return _restart_running_server(args, ref, commit, previous=recorded)


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
    down_since: Optional[float] = None
    while True:
        try:
            doc = client.get(base, "/api/fleet/update", timeout=10.0) or {}
        except client.ClientError:
            # This computer restarting at the very end — but not for ever.
            now = time.monotonic()
            if down_since is None:
                down_since = now
            elif now - down_since > RESTART_WAIT_S:
                _err(
                    "the server here stopped answering for %ds — see how the "
                    "update went in Settings → Your devices once it's back, or check "
                    "its log" % int(RESTART_WAIT_S)
                )
                return 1
            time.sleep(POLL_S)
            continue
        down_since = None
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
