"""The ``mindflock`` console entry point.

Installed via ``[project.scripts]``::

    mindflock                     # serve (localhost only, port 8765)
    mindflock serve tailscale     # serve on the tailnet (phone access)
    mindflock serve --port 9000   # custom port
    mindflock serve --setup       # …run the guided setup first, then serve
    mindflock init                # guided first run: deps, agent login, repo
    mindflock init --yes          # …taking every default, for scripts
    mindflock doctor              # dependency preflight (exit 1 on failures)
    mindflock doctor --fix        # …and offer to run each fix command

    mindflock new [REPO_PATH] -p "…"   # create a session on a running server
    mindflock ls                  # list sessions (table or --json)
    mindflock accounts            # list auth profiles (Claude accounts, OpenRouter keys)
    mindflock accounts add work   # …add one; `login work` authenticates it
    mindflock accounts use work   # …make it the default for new sessions
    mindflock attach TITLE        # tmux attach to a session's terminal
    mindflock rm TITLE [--yes]    # end a session (keeps the worktree)
    mindflock open TITLE          # open the session workspace in the IDE
    mindflock events [--follow]   # print the /api/events stream

    mindflock uninstall           # undo MindFlock's writes to your repos
    mindflock uninstall --purge   # …and delete ~/.mindflock[-assistant] too

``serve`` delegates to :func:`backend.web.run.main` (the same code path as
``./backend/web/run.sh``); ``doctor`` runs the same checks as
``GET /api/doctor`` and prints them with per-platform fixes; ``init``
(:mod:`backend.init_wizard`) walks a brand-new user through those checks, offers
the doctor's own fix commands, and remembers the repo they pick — none of the
three needs a running server. The session
commands (J1) are thin clients over a *running* server's HTTP API — discovery
order is ``--host``/``--port`` → ``MINDFLOCK_HOST``/``MINDFLOCK_PORT`` →
probe 127.0.0.1:8765 (see :mod:`backend.client`). They never spawn an
engine of their own, so the terminal and the web UI stay one system.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Callable, List, Optional, TextIO

from backend import __version__, client

if TYPE_CHECKING:
    from backend.doctor import Check

__all__ = ["main", "print_checks"]

# Terminal glyph per doctor status (see backend.doctor for the semantics).
_GLYPHS = {"ok": "✓", "info": "-", "warn": "!", "fail": "✗"}

#: How long `mindflock new` waits for the session to leave "loading".
_NEW_WAIT_S = 15.0
_NEW_POLL_S = 1.0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mindflock",
        description=(
            "MindFlock — a private flock of AI coding agents, started by your "
            "ticket queue, merged by you."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser(
        "serve",
        help="start the MindFlock server (default command); run it from the git repo you want to manage",
    )
    serve.add_argument(
        "mode",
        nargs="?",
        default=None,
        choices=("local", "tailscale"),
        help="local = bind 127.0.0.1 (default); tailscale = bind 0.0.0.0 (phone/tailnet access, auth gate on)",
    )
    serve.add_argument("--port", type=int, default=None, help="port (default 8765)")
    serve.add_argument(
        "--setup",
        action="store_true",
        help="run the guided first-run setup (see `mindflock init`) before binding the port",
    )

    init_p = sub.add_parser(
        "init",
        help="guided first-run setup: check dependencies, log in your agent CLI, pick your repo",
    )
    init_p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="take every default without prompting (for scripts)",
    )

    doctor_p = sub.add_parser(
        "doctor",
        help="check git/tmux/agent-CLI (plus optional gh, uv, tailscale) and print fixes",
    )
    doctor_p.add_argument(
        "--fix",
        action="store_true",
        help="offer to run each fix command interactively (installs missing deps for you)",
    )

    # Shared --host/--port for every command that talks to a running server.
    server_opts = argparse.ArgumentParser(add_help=False)
    server_opts.add_argument(
        "--host",
        default=None,
        help="server host (default: $MINDFLOCK_HOST or 127.0.0.1)",
    )
    server_opts.add_argument(
        "--port",
        type=int,
        default=None,
        help="server port (default: $MINDFLOCK_PORT or 8765)",
    )

    new = sub.add_parser(
        "new",
        parents=[server_opts],
        help="create a session on the running server (repo defaults to CWD)",
    )
    new.add_argument(
        "repo_path",
        nargs="?",
        default=None,
        metavar="REPO_PATH",
        help="repo folder for the session (default: current directory)",
    )
    new.add_argument(
        "-p", "--prompt", default="", help="seed prompt typed into the agent"
    )
    new.add_argument(
        "-t", "--title", default=None, help="session name (default: repo basename)"
    )
    new.add_argument(
        "--provision",
        action="store_true",
        help="run repo setup / warm test caches (provisioned mode)",
    )
    new.add_argument(
        "--strategy",
        choices=("worktree", "clone"),
        default="worktree",
        help="workspace strategy for --provision (default: worktree)",
    )
    new.add_argument(
        "--program", default="", help="agent program (default: server's default)"
    )
    new.add_argument(
        "--account",
        default="",
        metavar="ID",
        help="auth profile the session runs under (see `mindflock accounts`; "
        "'default' = the CLI's own login; unset = the app-wide default)",
    )

    ls = sub.add_parser(
        "ls", parents=[server_opts], help="list sessions on the running server"
    )
    ls.add_argument(
        "--json", action="store_true", dest="as_json", help="raw JSON for scripting"
    )

    attach = sub.add_parser(
        "attach",
        parents=[server_opts],
        help="attach the terminal to a session's tmux (unambiguous title prefix ok)",
    )
    attach.add_argument("title", metavar="TITLE")

    rm = sub.add_parser(
        "rm",
        parents=[server_opts],
        help="end a session (keeps the worktree); prompts unless --yes",
    )
    rm.add_argument("title", metavar="TITLE")
    rm.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="skip the confirmation prompt (for scripts)",
    )

    open_ = sub.add_parser(
        "open",
        parents=[server_opts],
        help="open a session's workspace in the configured IDE",
    )
    open_.add_argument("title", metavar="TITLE")

    events = sub.add_parser(
        "events",
        parents=[server_opts],
        help="print the session-event stream (backlog; --follow keeps streaming)",
    )
    events.add_argument(
        "--follow", "-f", action="store_true", help="keep streaming new events"
    )

    accounts = sub.add_parser(
        "accounts",
        parents=[server_opts],
        help="manage auth profiles (multiple Claude accounts, OpenRouter keys) and hot-swap between them",
        description=(
            "Auth profiles let sessions run under different identities — a "
            "personal Claude subscription next to a work one, or an OpenRouter "
            "key with its own model — without logging the CLI out and back in. "
            "Changes go through the running server when there is one (so the "
            "app picks them up immediately) and fall back to "
            "~/.mindflock/settings.json otherwise."
        ),
    )
    acc_sub = accounts.add_subparsers(dest="accounts_command")
    acc_sub.add_parser(
        "ls", parents=[server_opts], help="list configured accounts (the default)"
    )
    acc_add = acc_sub.add_parser(
        "add", parents=[server_opts], help="add an account/key profile"
    )
    acc_add.add_argument("id", metavar="ID", help="short slug (e.g. work, personal)")
    acc_add.add_argument(
        "--kind",
        choices=("account", "api_key", "openrouter"),
        default="account",
        help="account = a separate CLI login (own config dir); api_key = a vendor "
        "API key; openrouter = an OpenRouter key (default: account)",
    )
    acc_add.add_argument(
        "--agent",
        dest="provider",
        default="",
        metavar="CLI",
        help="which agent CLI this profile authenticates (default: claude; "
        "openrouter profiles apply to any CLI with an OpenRouter route)",
    )
    acc_add.add_argument("--label", default="", help="display name (e.g. 'Work')")
    acc_add.add_argument(
        "--key",
        default="",
        metavar="API_KEY",
        help="API key (api_key/openrouter kinds)",
    )
    acc_add.add_argument(
        "--model", default="", help="model pin (e.g. anthropic/claude-sonnet-4.5)"
    )
    acc_add.add_argument(
        "--base-url", default="", dest="base_url", help="alternate endpoint URL"
    )
    acc_add.add_argument(
        "--config-dir",
        default="",
        dest="config_dir",
        help="account kind: explicit config dir (default: ~/.mindflock/accounts/ID)",
    )
    acc_login = acc_sub.add_parser(
        "login",
        parents=[server_opts],
        help="run the CLI's own login for an account profile (interactive)",
    )
    acc_login.add_argument("id", metavar="ID")
    acc_use = acc_sub.add_parser(
        "use",
        parents=[server_opts],
        help="make an account the app-wide default ('default' = the CLI's own login)",
    )
    acc_use.add_argument("id", metavar="ID")
    acc_rm = acc_sub.add_parser(
        "rm", parents=[server_opts], help="remove an account profile"
    )
    acc_rm.add_argument("id", metavar="ID")

    uninstall = sub.add_parser(
        "uninstall",
        parents=[server_opts],
        help="remove MindFlock's worktrees, hooks and scratch files from your repos",
        description=(
            "Undo what MindFlock wrote outside its own venv: session worktrees "
            "(removed through git so your repos stay consistent), the activity "
            "hooks merged into your repos' .claude/.codex settings, the "
            ".mindflock_* scratch files and their .git/info/exclude lines. "
            "Add --purge to also delete ~/.mindflock and ~/.mindflock-assistant. "
            "Finish by running the `uv tool uninstall mindflock` line this "
            "prints — it can't be run from inside the venv it deletes."
        ),
    )
    uninstall.add_argument(
        "--purge",
        action="store_true",
        help="also delete ~/.mindflock and ~/.mindflock-assistant (settings, state, usage history)",
    )
    uninstall.add_argument(
        "--keep-worktrees",
        action="store_true",
        help="leave session worktrees and branches in place (only clean hooks/scratch files)",
    )
    uninstall.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        dest="dry_run",
        help="print what would be removed and exit without changing anything",
    )
    uninstall.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="skip the confirmation prompt (for scripts)",
    )

    return parser


def _cmd_serve(mode: Optional[str], port: Optional[int], setup: bool = False) -> int:
    from backend.web.run import main as serve_main

    argv: List[str] = []
    if mode:
        argv.append(mode)
    if port is not None:
        argv.append(str(port))
    if setup:
        # run.py owns the ordering (setup runs after its double-launch guard,
        # before the bind), so pass the intent along as one of its tokens.
        argv.append("--setup")
    serve_main(argv)  # prints the friendly web-deps hint itself if they're missing
    return 0


def print_checks(checks: list[Check], stream: Optional[TextIO] = None) -> None:
    """Render doctor results the one way MindFlock renders them: a glyph column,
    label-aligned details, and the fix line under anything that needs attention.

    Shared by ``mindflock doctor`` and the first-run wizard
    (:mod:`backend.init_wizard`) so the two can never drift into two dialects of
    the same table. An empty list prints nothing — the wizard's report passes
    only the checks that need attention, and on a healthy machine that is none.
    """
    out = stream if stream is not None else sys.stdout
    if not checks:
        return
    width = max(len(c.label) for c in checks)
    for c in checks:
        glyph = _GLYPHS.get(c.status, "?")
        print(f"  {glyph} {c.label.ljust(width)}  {c.detail}", file=out)
        if c.fix and c.status in ("warn", "fail"):
            print(f"    {' ' * width}  fix: {c.fix}", file=out)


def _cmd_init(assume_yes: bool = False) -> int:
    from backend import init_wizard

    return init_wizard.run(assume_yes=assume_yes)


def _cmd_doctor(fix: bool = False) -> int:
    from backend import doctor

    checks = doctor.run_checks()
    print("MindFlock doctor")
    print()
    print_checks(checks)
    if fix:
        checks = _fix_checks(checks)
    failed = [c for c in checks if c.status == "fail"]
    print()
    if failed:
        print(
            f"{len(failed)} required dependenc{'y' if len(failed) == 1 else 'ies'} missing."
        )
        return 1
    print("All required dependencies look good.")
    return 0


def _fix_checks(checks: list[Check]) -> list[Check]:
    """Interactive `doctor --fix` loop: for each warn/fail check that carries a
    runnable fix command, ask, run it with inherited stdio (so interactive
    installers like `gh auth login` work), then re-probe just that check.
    Returns the checks list with re-probed results swapped in."""
    from backend import doctor

    fixable = [c for c in checks if c.status in ("warn", "fail") and c.cmd]
    if not fixable:
        return checks
    if not sys.stdin.isatty():
        print()
        print(
            "--fix needs an interactive terminal to confirm each command; "
            "run `mindflock doctor --fix` yourself, or paste the fix lines above."
        )
        return checks
    checks = list(checks)
    print()
    print(f"{len(fixable)} fixable — I can run each command for you (Enter = yes).")
    for c in fixable:
        try:
            answer = input(f"\n  {c.label}: run `{c.cmd}`? [Y/n] ").strip().lower()
        except EOFError:
            break
        if answer not in ("", "y", "yes"):
            print("  skipped")
            continue
        # shell=True: fix commands are trusted strings we authored (pipes like
        # the uv installer need a shell); stdio is inherited for interactivity.
        proc = subprocess.run(c.cmd, shell=True)
        recheck = doctor.CHECKS_BY_ID.get(c.id)
        if proc.returncode != 0:
            print(f"  command exited {proc.returncode}")
        if recheck is None:
            continue
        try:
            fresh = recheck()
        except Exception:  # noqa: BLE001 — a broken re-probe shouldn't kill the loop
            continue
        checks[checks.index(c)] = fresh
        glyph = _GLYPHS.get(fresh.status, "?")
        print(f"  {glyph} {fresh.label}  {fresh.detail}")
        if fresh.status in ("warn", "fail"):
            print(
                "  still not healthy — you may need to open a new shell (PATH) "
                "or follow the docs link" + (f": {fresh.docs}" if fresh.docs else ".")
            )
    return checks


# --------------------------------------------------------------------------- #
# J1 session commands (thin clients over a running server's API)
# --------------------------------------------------------------------------- #
def _auto_title(repo_path: str, existing: List[str]) -> str:
    """Default session title: sanitized repo basename, ``-2``/``-3``… suffixed
    until it doesn't collide with an existing session."""
    base = os.path.basename(os.path.normpath(repo_path)) or "session"
    # Keep it tmux/branch-friendly: letters, digits, . _ - (spaces would be
    # stripped by the tmux sanitizer anyway; '/' would trigger branch parsing).
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-.") or "session"
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _resolve_title(instances: List[dict], needle: str) -> dict:
    """Find a session by exact title, else by unambiguous prefix.

    Raises ``client.ClientError`` with a user-facing message otherwise."""
    by_title = {str(i.get("title", "")): i for i in instances}
    if needle in by_title:
        return by_title[needle]
    matches = [t for t in by_title if t.startswith(needle)]
    if len(matches) == 1:
        return by_title[matches[0]]
    if not matches:
        raise client.ClientError("no session named %r (run `mindflock ls`)" % needle)
    raise client.ClientError(
        "ambiguous title %r — matches: %s" % (needle, ", ".join(sorted(matches)))
    )


def _cmd_new(args: argparse.Namespace) -> int:
    base = client.discover(args.host, args.port)
    repo = os.path.abspath(os.path.expanduser(args.repo_path or os.getcwd()))
    instances = client.get(base, "/api/instances") or []
    title = (args.title or "").strip() or _auto_title(
        repo, [str(i.get("title", "")) for i in instances]
    )
    payload = {
        "title": title,
        "repo_path": repo,
        "program": args.program or "",
        "prompt": args.prompt or "",
    }
    if getattr(args, "account", ""):
        payload["profile_id"] = args.account
    if args.provision:
        payload["provisioned"] = True
        payload["workspace_strategy"] = args.strategy
    created = client.post(base, "/api/instances", payload)
    title = str((created or {}).get("title") or title)  # server may re-derive it
    print("created session %s" % title)
    print("  attach:  mindflock attach %s" % title)

    # The server returns 202 and does the heavy lifting (worktree + tmux +
    # provisioning) in the background; poll briefly so failures are visible.
    deadline = time.monotonic() + _NEW_WAIT_S
    status = "loading"
    while time.monotonic() < deadline:
        time.sleep(_NEW_POLL_S)
        try:
            listing = client.get(base, "/api/instances") or []
        except client.ClientError:
            continue
        match = [i for i in listing if i.get("title") == title]
        if not match:
            print(
                "session %s failed to start — check the server logs" % title,
                file=sys.stderr,
            )
            return 1
        status = str(match[0].get("status", ""))
        if status != "loading":
            break
    if status == "loading":
        print("still provisioning (status: loading) — watch it with `mindflock ls`")
    else:
        print("ready (status: %s)" % status)
    return 0


def _fmt_diff(inst: dict) -> str:
    """`+n −m` from the optional ``diff_stat`` field; "" when absent."""
    ds = inst.get("diff_stat")
    if not isinstance(ds, dict):
        return ""
    return "+%s −%s" % (ds.get("additions", 0), ds.get("deletions", 0))


def _fmt_cost(inst: dict) -> str:
    cost = inst.get("tokens_cost")
    if not isinstance(cost, (int, float)) or not cost:
        return ""
    return "$%.2f" % cost


def _render_table(rows: List[List[str]], headers: List[str]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    for row in rows:
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip())
    return "\n".join(lines)


def _cmd_ls(args: argparse.Namespace) -> int:
    base = client.discover(args.host, args.port)
    instances = client.get(base, "/api/instances") or []
    if args.as_json:
        print(json.dumps(instances, indent=2))
        return 0
    if not instances:
        print("no sessions — create one with `mindflock new`")
        return 0
    headers = ["TITLE", "REPO", "STATUS", "ACTIVITY", "STAGE", "DIFF", "COST"]
    rows = [
        [
            str(i.get("title", "")),
            str(i.get("repo", "")),
            str(i.get("status", "")),
            str(i.get("activity", "")),
            str(i.get("stage", "")),
            _fmt_diff(i),
            _fmt_cost(i),
        ]
        for i in instances
    ]
    print(_render_table(rows, headers))
    return 0


def _stdout_is_tty() -> bool:
    """True when stdout is a real terminal (tmux attach needs one)."""
    try:
        return bool(sys.stdout.isatty())
    except Exception:  # noqa: BLE001 — exotic stdout replacements
        return False


def _cmd_attach(args: argparse.Namespace) -> int:
    # tmux attach-session inside a pipe/script just errors cryptically —
    # catch it up front with a pointer to the scriptable alternative.
    if not _stdout_is_tty():
        print(
            "attach needs a real terminal (running in a script? use `mindflock ls --json`)",
            file=sys.stderr,
        )
        return 1
    base = client.discover(args.host, args.port)
    inst = _resolve_title(client.get(base, "/api/instances") or [], args.title)
    tmux_name = str(inst.get("tmux_name") or "")
    if not tmux_name:  # very old server without the field — derive it
        from backend.session.tmux.tmux import to_mindflock_tmux_name

        tmux_name = to_mindflock_tmux_name(str(inst.get("title", "")))
    if not shutil.which("tmux"):
        print(
            "tmux not found on PATH — run `mindflock doctor` for install hints",
            file=sys.stderr,
        )
        return 1
    # Replace this process with tmux so the user lands in the live session.
    os.execvp("tmux", ["tmux", "attach-session", "-t", tmux_name])
    return 0  # pragma: no cover — execvp does not return


def _cmd_rm(args: argparse.Namespace) -> int:
    """End a session on the running server (DELETE /api/instances/{title}).

    The worktree stays on disk (recoverable via the web UI's Recently closed /
    Disk manager). Prompts for confirmation unless ``--yes``; ``TITLE`` may be
    any unambiguous prefix, like ``attach``."""
    base = client.discover(args.host, args.port)
    inst = _resolve_title(client.get(base, "/api/instances") or [], args.title)
    title = str(inst.get("title", ""))
    if not args.yes:
        try:
            answer = input("End session %r? Its worktree is kept. [y/N] " % title)
        except (EOFError, KeyboardInterrupt):
            print("aborted", file=sys.stderr)
            return 1
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 0
    client.delete(base, "/api/instances/%s" % title)
    print("removed session %s (worktree kept)" % title)
    return 0


def _cmd_open(args: argparse.Namespace) -> int:
    base = client.discover(args.host, args.port)
    inst = _resolve_title(client.get(base, "/api/instances") or [], args.title)
    title = str(inst.get("title", ""))
    result = client.post(base, "/api/instances/%s/ide" % title) or {}
    if result.get("opened_new"):
        print("opened %s in the IDE" % title)
    else:
        print("focused the IDE window for %s" % title)
    return 0


def _cmd_events(args: argparse.Namespace) -> int:
    base = client.discover(args.host, args.port)
    try:
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect
    except ModuleNotFoundError:
        print(
            "`mindflock events` needs the websockets package.\n"
            "Reinstall with the web extra:  "
            'uv tool install --force "mindflock[web] @ '
            'git+https://github.com/MindFlock/MindFlock"\n'
            "  (or, in a source checkout:  uv sync --group web)",
            file=sys.stderr,
        )
        return 1

    url = client.ws_url(base, "/api/events")
    try:
        with connect(url) as ws:
            while True:
                try:
                    # Backlog arrives immediately; without --follow we stop at
                    # the first quiet second instead of streaming forever.
                    raw = ws.recv(timeout=None if args.follow else 1.0)
                except TimeoutError:
                    break
                print(_format_event(json.loads(raw)), flush=True)
    except ConnectionClosed:
        # A peer-initiated close is the normal way a --follow stream ends
        # (e.g. the server restarts); exit cleanly like KeyboardInterrupt.
        return 0
    except KeyboardInterrupt:
        return 0
    except OSError as err:
        print("event stream failed: %s" % err, file=sys.stderr)
        return 1
    return 0


def _format_event(env: dict) -> str:
    """One line per envelope: `HH:MM:SS event session old -> new`."""
    ts = env.get("ts")
    clock = (
        time.strftime("%H:%M:%S", time.localtime(ts))
        if isinstance(ts, (int, float))
        else "--:--:--"
    )
    parts = [clock, str(env.get("event", "?"))]
    if env.get("session"):
        parts.append(str(env["session"]))
    old, new = env.get("old"), env.get("new")
    if old is not None or new is not None:
        parts.append(
            "%s -> %s"
            % (old if old is not None else "·", new if new is not None else "·")
        )
    data = env.get("data")
    if data:
        parts.append(json.dumps(data, separators=(",", ":"), default=str))
    return "  ".join(parts)


def _cmd_uninstall(args: argparse.Namespace) -> int:
    """Remove MindFlock's footprint outside its venv (see :mod:`backend.uninstall`).

    Runs offline against ``state.json`` — no server needed, and in fact refused
    while one is up, since tearing down worktrees under live sessions would
    leave the engine writing into deleted directories.
    """
    from backend import uninstall as uninstall_mod

    # A dry run changes nothing, so it stays allowed while a server is up —
    # that's exactly when someone wants to preview what uninstalling would do.
    if uninstall_mod.server_is_running(args.host, args.port):
        if not args.dry_run:
            print(
                "a MindFlock server is running — stop it first (close the desktop app,\n"
                "or Ctrl-C `mindflock serve`) so sessions aren't torn down underneath it.\n"
                "To preview without changing anything: mindflock uninstall --dry-run",
                file=sys.stderr,
            )
            return 1
        print(
            "note: a server is running — this preview is a snapshot, not a plan to apply as-is."
        )

    plan = uninstall_mod.build_plan()
    for warning in plan.warnings:
        print("warning: %s" % warning, file=sys.stderr)

    removable = [s for s in plan.sessions if s.removable_worktree]
    print("MindFlock uninstall")
    print()
    print("  sessions recorded:    %d" % len(plan.sessions))
    if not args.keep_worktrees:
        print("  worktrees to remove:  %d" % len(removable))
        print("  orphaned worktrees:   %d" % len(plan.orphan_worktrees))
    print("  repos to clean:       %d" % len(plan.workdirs))
    if args.purge:
        for path in plan.purge_dirs:
            print("  purge:                %s" % path)
    else:
        print(
            "  keeping:              %s" % (", ".join(uninstall_mod.home_dirs()) or "—")
        )
    print()

    if not args.dry_run and not args.yes:
        what = "Remove the items above"
        if args.purge:
            what += " AND delete your settings, state and usage history"
        try:
            answer = input("%s? [y/N] " % what)
        except (EOFError, KeyboardInterrupt):
            print("aborted", file=sys.stderr)
            return 1
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 0

    report = uninstall_mod.execute(
        plan,
        purge=args.purge,
        dry_run=args.dry_run,
        keep_worktrees=args.keep_worktrees,
    )
    for line in report.actions:
        print("  %s" % line)
    if not report.actions:
        print("  nothing to do")
    for line in report.errors:
        print("  ! %s" % line, file=sys.stderr)

    print()
    if args.dry_run:
        print("Dry run — nothing was changed. Re-run without --dry-run to apply.")
        return 0
    print("Done. Final step (can't run from inside the venv it deletes):")
    print("  uv tool uninstall mindflock")
    if not args.purge:
        print()
        print(
            "Your settings and history are still in %s."
            % (" and ".join(uninstall_mod.home_dirs()) or "no MindFlock home directory")
        )
        print("Re-run with --purge to delete those too.")
    return 1 if report.errors else 0


# --------------------------------------------------------------------------- #
# accounts — auth profiles (multiple Claude accounts / OpenRouter keys)
# --------------------------------------------------------------------------- #
_ACCOUNT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _accounts_server(args) -> Optional[str]:
    """Base URL of a running server, or None (work on the local store then).

    Going through the server when one is up matters: it caches the parsed
    settings for the life of its process, so a store edited behind its back
    would not be seen until restart.
    """
    try:
        return client.discover(getattr(args, "host", None), getattr(args, "port", None))
    except client.ServerNotFound:
        return None


def _accounts_local_profiles() -> list[dict]:
    from backend.config import settings as settings_store

    settings_store.invalidate()  # another process may have written the store
    return [p.to_dict() for p in settings_store.load_settings().auth_profiles.profiles]


def _accounts_view(base: Optional[str]) -> dict:
    if base is not None:
        return client.get(base, "/api/settings/auth-profiles") or {}
    from backend.config import settings as settings_store

    settings_store.invalidate()
    s = settings_store.load_settings().auth_profiles
    return {
        "profiles": [p.to_dict() for p in s.profiles],
        "default_profile": s.default_profile,
    }


def _cmd_accounts_ls(args) -> int:
    view = _accounts_view(_accounts_server(args))
    profiles = view.get("profiles") or []
    default = view.get("default_profile") or ""
    if not profiles:
        print("No accounts configured.")
        print()
        print("  mindflock accounts add work --label 'Work'   # a second Claude login")
        print("  mindflock accounts login work                # authenticate it")
        print(
            "  mindflock accounts add or --kind openrouter --key sk-or-… --model MODEL"
        )
        return 0
    width = max(len(p.get("id", "")) for p in profiles)
    kind_w = max(len(p.get("kind", "")) for p in profiles)
    for p in profiles:
        marker = "*" if p.get("id") == default else " "
        agent = p.get("provider") or (
            "any" if p.get("kind") == "openrouter" else "claude"
        )
        cols = [p.get("kind", "").ljust(kind_w), agent.ljust(6)]
        if p.get("model"):
            cols.append(p["model"])
        if p.get("label"):
            cols.append(p["label"])
        print("%s %s  %s" % (marker, p.get("id", "").ljust(width), "  ".join(cols)))
    print()
    print(
        "'*' = default for new sessions. Switch with `mindflock accounts use ID` "
        "('default' = the CLI's own login); per-session via the app or "
        "`mindflock new --account ID`."
    )
    return 0


def _cmd_accounts_add(args) -> int:
    pid = args.id.strip().lower()
    if not _ACCOUNT_ID_RE.match(pid):
        print(
            "error: id must be lowercase letters/digits/-/_ (max 64)",
            file=sys.stderr,
        )
        return 1
    if args.kind in ("api_key", "openrouter") and not args.key:
        print("error: --kind %s needs --key" % args.kind, file=sys.stderr)
        return 1
    profile = {
        "id": pid,
        "kind": args.kind,
        "label": args.label,
        "provider": args.provider.strip().lower(),
        "api_key": args.key,
        "model": args.model,
        "base_url": args.base_url,
        "config_dir": args.config_dir,
    }
    profile = {k: v for k, v in profile.items() if v}
    profile["kind"] = args.kind  # kind always rides along, even the default
    base = _accounts_server(args)
    if base is not None:
        view = client.get(base, "/api/settings/auth-profiles") or {}
        existing = view.get("profiles") or []
        if any(p.get("id") == pid for p in existing):
            print("error: account '%s' already exists" % pid, file=sys.stderr)
            return 1
        client.put(
            base, "/api/settings/auth-profiles", {"profiles": existing + [profile]}
        )
    else:
        from backend.config import settings as settings_store

        existing = _accounts_local_profiles()
        if any(p.get("id") == pid for p in existing):
            print("error: account '%s' already exists" % pid, file=sys.stderr)
            return 1
        settings_store.set_auth_profiles(existing + [profile])
    print("Added account '%s' (%s)." % (pid, args.kind))
    if args.kind == "account":
        print("Next: `mindflock accounts login %s` to authenticate it." % pid)
    print("Make it the default with `mindflock accounts use %s`." % pid)
    return 0


def _cmd_accounts_login(args) -> int:
    """Run the CLI's own login flow under the profile's isolation env, in THIS
    terminal — OAuth login is interactive, so it cannot go through the API."""
    from backend.providers import auth_profiles

    pid = args.id.strip().lower()
    profile = auth_profiles.get_profile(pid)
    if profile is None:
        print("error: no account '%s' — `mindflock accounts ls`" % pid, file=sys.stderr)
        return 1
    env = auth_profiles.login_env(profile)
    if not env:
        print(
            "error: '%s' is a %s profile — only 'account' profiles have a "
            "login flow (keys are injected at launch)" % (pid, profile.kind),
            file=sys.stderr,
        )
        return 1
    os.makedirs(auth_profiles.account_dir(profile), mode=0o700, exist_ok=True)
    from backend import providers

    try:
        cmd = providers.resolve(profile.resolved_provider()).login_command()
    except Exception:  # noqa: BLE001
        cmd = ""
    cmd = cmd or profile.resolved_provider()
    print(
        "Logging '%s' in via `%s` (%s)…"
        % (pid, cmd, ", ".join("%s=%s" % (k, v) for k, v in sorted(env.items())))
    )
    # shell=True: login commands are provider-authored strings (`claude /login`);
    # stdio is inherited so the OAuth flow is fully interactive.
    proc = subprocess.run(cmd, shell=True, env={**os.environ, **env})
    if proc.returncode == 0:
        print()
        print("Done. Run sessions on it with `mindflock accounts use %s`," % pid)
        print("or pick it per-session in the app's New dialog / session header.")
    return proc.returncode


def _cmd_accounts_use(args) -> int:
    pid = args.id.strip().lower()
    target = "" if pid == "default" else pid
    base = _accounts_server(args)
    if base is not None:
        client.post(
            base, "/api/settings", {"auth_profiles": {"default_profile": target}}
        )
    else:
        from backend.config import settings as settings_store

        if target and not any(
            p.get("id") == target for p in _accounts_local_profiles()
        ):
            print("error: no account '%s'" % pid, file=sys.stderr)
            return 1
        settings_store.update_settings(auth_profiles={"default_profile": target})
    if target:
        print("New sessions now run as '%s'." % target)
    else:
        print("New sessions now use each CLI's own login (no profile).")
    print("Already-running sessions keep their identity until swapped or relaunched.")
    return 0


def _cmd_accounts_rm(args) -> int:
    pid = args.id.strip().lower()
    base = _accounts_server(args)
    if base is not None:
        view = client.get(base, "/api/settings/auth-profiles") or {}
        existing = view.get("profiles") or []
        kept = [p for p in existing if p.get("id") != pid]
        if len(kept) == len(existing):
            print("error: no account '%s'" % pid, file=sys.stderr)
            return 1
        client.put(base, "/api/settings/auth-profiles", {"profiles": kept})
    else:
        from backend.config import settings as settings_store

        existing = _accounts_local_profiles()
        kept = [p for p in existing if p.get("id") != pid]
        if len(kept) == len(existing):
            print("error: no account '%s'" % pid, file=sys.stderr)
            return 1
        settings_store.set_auth_profiles(kept)
    print(
        "Removed '%s'. Its config dir (if any) is kept — delete it yourself "
        "once you're sure." % pid
    )
    return 0


def _cmd_accounts(args) -> int:
    handler = {
        None: _cmd_accounts_ls,
        "ls": _cmd_accounts_ls,
        "add": _cmd_accounts_add,
        "login": _cmd_accounts_login,
        "use": _cmd_accounts_use,
        "rm": _cmd_accounts_rm,
    }.get(getattr(args, "accounts_command", None))
    if handler is None:  # unreachable via argparse, defensive
        return _cmd_accounts_ls(args)
    try:
        return handler(args)
    except client.ClientError as err:
        print("error: %s" % err, file=sys.stderr)
        return 1


_SESSION_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "new": _cmd_new,
    "ls": _cmd_ls,
    "attach": _cmd_attach,
    "rm": _cmd_rm,
    "open": _cmd_open,
    "events": _cmd_events,
}


def main(argv: Optional[List[str]] = None) -> int:
    """Parse ``argv`` and dispatch to the matching subcommand; return the exit code.

    ``doctor`` and the J1 session commands (new/ls/attach/rm/open/events) run
    in-process; any other invocation — including no subcommand at all — falls
    through to ``serve``. A session command that can't reach a server turns the
    :class:`client.ServerNotFound` / :class:`client.ClientError` into a one-line
    stderr message and exit 1, never a traceback.
    """
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "doctor":
        return _cmd_doctor(fix=args.fix)
    if args.command == "init":
        # Like doctor and uninstall, not a session command: init is what you run
        # *before* there is a server, so it must never go through the
        # ServerNotFound handler below.
        return _cmd_init(assume_yes=args.yes)
    if args.command == "uninstall":
        # Not a session command: it works offline against state.json (and
        # refuses to run while a server is up), so it must never be wrapped in
        # the ServerNotFound handler below.
        return _cmd_uninstall(args)
    if args.command == "accounts":
        # Not a session command either: it prefers a running server but falls
        # back to the local settings store, so ServerNotFound is a routing
        # decision here, not an error.
        return _cmd_accounts(args)
    handler: Optional[Callable[[argparse.Namespace], int]] = _SESSION_COMMANDS.get(
        args.command or ""
    )
    if handler is not None:
        try:
            return handler(args)
        except client.ServerNotFound as err:
            print(str(err), file=sys.stderr)
            return 1
        except client.ClientError as err:
            print("error: %s" % err, file=sys.stderr)
            return 1
    # Default (no subcommand) = serve with defaults.
    mode = getattr(args, "mode", None)
    port = getattr(args, "port", None)
    return _cmd_serve(mode, port, setup=bool(getattr(args, "setup", False)))


if __name__ == "__main__":
    raise SystemExit(main())
