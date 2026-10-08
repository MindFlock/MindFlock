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
    mindflock msg TITLE "text…"   # message a session's agent (typed in when idle)
    mindflock inbox TITLE [--all] # read a session's messages (doesn't mark them read)

    mindflock peer status         # peer links: pair-code with another MindFlock user
    mindflock peer invite         # …make a one-time invite code (you listen)
    mindflock peer join CODE      # …pair using their code (you dial)
    mindflock peer share LINK REPO [--branch B] [--program P]   # share ONE folder
    mindflock peer export LINK TARGET_REPO peer/BRANCH          # bring work home

    mindflock devices             # your devices (settings follow you between them)
    mindflock devices add         # …one-time code for a new computer
    mindflock devices join DEV [CODE]  # …join DEV's group (no code = ask, wait for approval)
    mindflock devices cancel      # …stop asking to join
    mindflock devices approve DEV # …let a computer that asked join (deny DEV refuses)
    mindflock devices remove DEV  # …take one out (the rest get a new key); leave = this one

    mindflock mcp                 # MCP stdio server (lets agents reach other sessions)
    mindflock mcp --print-config  # …the snippets to register it in Claude/Codex

    mindflock uninstall          # undo MindFlock's writes to your repos
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

``mcp`` runs the MindFlock MCP stdio server (:mod:`backend.mcp`) in this
process — the same server MindFlock attaches to Claude/Codex sessions — and
speaks only MCP on stdout; ``--print-config`` prints how to register it with
your own client instead. ``msg``/``inbox`` are the human side of the same
mailbox: a message sent from the terminal carries ``from: ""`` (outside the
flock).
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
import urllib.parse
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, TextIO, Tuple

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
        help="install everything missing in one go (one confirmation), then offer the logins",
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

    # The same two options for a NESTED subcommand (``accounts`` is the only
    # two-level group). argparse applies a subparser's defaults over whatever
    # the outer parser already stored, so re-using ``server_opts`` on both
    # levels makes ``mindflock accounts --port 9999 ls`` silently reset the
    # port to None on the way into ``ls``. SUPPRESS writes the attribute only
    # when the flag is actually typed, so either position works and the
    # innermost one wins.
    server_opts_nested = argparse.ArgumentParser(add_help=False)
    server_opts_nested.add_argument(
        "--host", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    server_opts_nested.add_argument(
        "--port", type=int, default=argparse.SUPPRESS, help=argparse.SUPPRESS
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
        "ls",
        parents=[server_opts_nested],
        help="list configured accounts (the default)",
    )
    acc_add = acc_sub.add_parser(
        "add", parents=[server_opts_nested], help="add an account/key profile"
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
        parents=[server_opts_nested],
        help="run the CLI's own login for an account profile (interactive)",
    )
    acc_login.add_argument("id", metavar="ID")
    acc_use = acc_sub.add_parser(
        "use",
        parents=[server_opts_nested],
        help="make an account the app-wide default ('default' = the CLI's own login)",
    )
    acc_use.add_argument("id", metavar="ID")
    acc_rm = acc_sub.add_parser(
        "rm", parents=[server_opts_nested], help="remove an account profile"
    )
    acc_rm.add_argument("id", metavar="ID")

    msg = sub.add_parser(
        "msg",
        parents=[server_opts],
        help="send a message to a session's agent (typed into it when it is idle)",
    )
    msg.add_argument("title", metavar="TITLE", help="session (unambiguous prefix ok)")
    msg.add_argument(
        "text",
        nargs="+",
        metavar="TEXT",
        help="message text (several words are joined; a lone '-' reads stdin)",
    )
    msg.add_argument(
        "--delivery",
        choices=("auto", "inbox", "now"),
        default="auto",
        help="auto = type it in once the agent is idle (default); inbox = store "
        "only; now = type it immediately (never into an open dialog)",
    )

    inbox = sub.add_parser(
        "inbox",
        parents=[server_opts],
        help="list a session's messages without marking them read",
    )
    inbox.add_argument("title", metavar="TITLE", help="session (unambiguous prefix ok)")
    inbox.add_argument(
        "--all",
        action="store_true",
        dest="include_consumed",
        help="include messages already read or typed in (default: unread only)",
    )
    inbox.add_argument(
        "--json", action="store_true", dest="as_json", help="raw JSON for scripting"
    )

    peer = sub.add_parser(
        "peer",
        parents=[server_opts],
        help="peer links: pair-code with another MindFlock user in one sandboxed folder",
        description=(
            "Pair this MindFlock with another person's using a one-time code, "
            "then bind ONE shared folder per link: an agent runs in it inside a "
            "bubblewrap sandbox and talks to the peer's agent. See "
            "docs/peer-link.md. Needs a running server."
        ),
    )
    peer_sub = peer.add_subparsers(dest="peer_command")
    peer_sub.add_parser(
        "status", parents=[server_opts_nested], help="sandbox, listener, links"
    )
    p_inv = peer_sub.add_parser(
        "invite",
        parents=[server_opts_nested],
        help="create a one-time invite code (valid 10 minutes, single use)",
    )
    p_inv.add_argument("--ttl", type=int, default=None, help="seconds (60-600)")
    p_inv.add_argument(
        "--advertise",
        default=None,
        help="host/IP the peer dials (default: Tailscale IP, else LAN IP)",
    )
    p_join = peer_sub.add_parser(
        "join", parents=[server_opts_nested], help="pair using a peer's code"
    )
    p_join.add_argument("code", metavar="CODE")
    peer_sub.add_parser(
        "links", parents=[server_opts_nested], help="list links with their SAS"
    )
    p_unlink = peer_sub.add_parser(
        "unlink", parents=[server_opts_nested], help="remove a link"
    )
    p_unlink.add_argument("link", metavar="LINK", help="link id (unique prefix ok)")
    p_unlink.add_argument(
        "--delete-files", action="store_true", help="also delete the shared folder"
    )
    p_share = peer_sub.add_parser(
        "share",
        parents=[server_opts_nested],
        help="clone a repo into the link's shared folder and start its sandboxed session",
    )
    p_share.add_argument("link", metavar="LINK")
    p_share.add_argument("repo", metavar="REPO")
    p_share.add_argument("--branch", default=None)
    p_share.add_argument("--program", default=None, help="claude or codex")
    p_unshare = peer_sub.add_parser(
        "unshare", parents=[server_opts_nested], help="stop the shared session"
    )
    p_unshare.add_argument("link", metavar="LINK")
    p_unshare.add_argument(
        "--delete-files", action="store_true", help="also delete the shared folder"
    )
    p_export = peer_sub.add_parser(
        "export",
        parents=[server_opts_nested],
        help="checkpoint the shared folder and fetch it into your repo as a branch",
    )
    p_export.add_argument("link", metavar="LINK")
    p_export.add_argument("target_repo", metavar="TARGET_REPO")
    p_export.add_argument("branch", metavar="BRANCH", help="must start with peer/")
    p_addr = peer_sub.add_parser(
        "address",
        parents=[server_opts_nested],
        help="point a joined link at the inviter's new address (e.g. a new relay URL)",
    )
    p_addr.add_argument("link", metavar="LINK")
    p_addr.add_argument(
        "address", metavar="ADDRESS", help="host:port or wss://host/path"
    )

    devices = sub.add_parser(
        "devices",
        parents=[server_opts],
        help="your devices: join your other computers so settings follow you",
        description=(
            "Group the computers YOU own (found over Tailscale) so settings "
            "sync between them. A new computer joins with a code made on one "
            "already in the group (`devices add` there, `devices join DEVICE "
            "CODE` here), or by asking (`devices join DEVICE`) and being "
            "approved there. Needs a running server."
        ),
    )
    dev_sub = devices.add_subparsers(dest="devices_command")
    d_list = dev_sub.add_parser(
        "list",
        parents=[server_opts_nested],
        help="your devices, pending join requests, other computers you could join",
    )
    d_list.add_argument(
        "--json", action="store_true", dest="as_json", help="raw JSON for scripting"
    )
    d_add = dev_sub.add_parser(
        "add",
        parents=[server_opts_nested],
        help="make a one-time code for a new computer (or add DEVICE you paired with a token)",
    )
    d_add.add_argument(
        "device",
        nargs="?",
        default=None,
        metavar="DEVICE",
        help="add a device this one already holds an access token for — no code needed",
    )
    d_join = dev_sub.add_parser(
        "join",
        parents=[server_opts_nested],
        help="join DEVICE's group: with its CODE, or ask and wait for approval there",
    )
    d_join.add_argument("device", metavar="DEVICE", help="device name (or host)")
    # nargs="*" so a code typed with a space ("ABCD EFGH") still arrives whole;
    # the server normalizes spaces/dashes and look-alike letters away.
    d_join.add_argument("code", nargs="*", metavar="CODE", help="XXXX-XXXX")
    d_join.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="don't ask first (this computer takes DEVICE's shared settings)",
    )
    dev_sub.add_parser(
        "cancel",
        parents=[server_opts_nested],
        help="stop asking to join (the request is withdrawn on the other device too)",
    )
    for name, verb in (("approve", "let"), ("deny", "refuse")):
        d_ans = dev_sub.add_parser(
            name,
            parents=[server_opts_nested],
            help="%s a computer asking to join (by device name or request id)" % verb,
        )
        d_ans.add_argument("request", metavar="DEVICE|ID")
        if name == "approve":
            d_ans.add_argument(
                "--yes",
                "-y",
                action="store_true",
                help="skip the check-the-code confirmation",
            )
    d_rm = dev_sub.add_parser(
        "remove",
        parents=[server_opts_nested],
        help=(
            "take DEVICE out of your devices (the others get a new key, and "
            "every device's access token is replaced)"
        ),
    )
    d_rm.add_argument("device", metavar="DEVICE")
    d_rm.add_argument("--yes", "-y", action="store_true", help="don't ask first")
    d_rm.add_argument(
        "--keep-tokens",
        action="store_true",
        help=(
            "only re-key the group; keep every device's own access token "
            "(the removed device keeps any it was given)"
        ),
    )
    d_leave = dev_sub.add_parser(
        "leave",
        parents=[server_opts_nested],
        help="take THIS computer out of your devices (settings sync stops)",
    )
    d_leave.add_argument("--yes", "-y", action="store_true", help="don't ask first")

    mcp = sub.add_parser(
        "mcp",
        parents=[server_opts],
        help="run the MindFlock MCP stdio server (or --print-config to register it)",
        description=(
            "Serve the Model Context Protocol on stdin/stdout so an agent CLI can "
            "list, message, spawn and steer MindFlock sessions. MindFlock attaches "
            "it to the Claude/Codex sessions it starts; run --print-config to "
            "register it with your own client."
        ),
    )
    mcp.add_argument(
        "--scope",
        choices=("readonly", "children", "all"),
        default=None,
        help="what the server may steer: readonly, children (default: sessions "
        "it or its session spawned) or all",
    )
    mcp.add_argument(
        "--print-config",
        action="store_true",
        dest="print_config",
        help="print the Claude Code / Codex registration snippets and exit",
    )

    sub.add_parser(
        "token",
        help="print this machine's access token (what another device or the sign-in page asks for)",
        description=(
            "Print the access token this machine's MindFlock server checks: "
            "MINDFLOCK_AUTH_TOKEN when set, else the one persisted in "
            "~/.mindflock/settings.json. Paste it into the sign-in page or "
            'into another device\'s "Connect" dialog. Exits 1 when no token '
            "has been created yet (the server makes one the first time the "
            "access-token gate is on)."
        ),
    )

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


def _cmd_token(out: Optional[TextIO] = None) -> int:
    """``mindflock token``: print the local access token, or exit 1 without one."""
    tok = client._read_token()
    if not tok:
        print(
            "no access token yet — the server creates one the first time the "
            "access-token gate is on (Tailscale mode, or Settings → Security)",
            file=sys.stderr,
        )
        return 1
    print(tok, file=out or sys.stdout)
    return 0


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


def _install_all(checks: list[Check]) -> list[Check]:
    """The one-shot half of `doctor --fix`: show everything missing that this
    machine needs, ask ONCE, then run it as a single script (one package-manager
    run, so one sudo prompt) and re-probe every check. Returns the checks list,
    re-probed when the script ran."""
    from backend import doctor

    plan = doctor.install_plan(checks)
    steps = plan["steps"]
    if not steps:
        return checks
    print()
    print("Missing — I can install all of it in one go:")
    for st in steps:
        print(f"  • {st['label']}")
        print(f"      {st['cmd']}")
    try:
        answer = input(f"\nInstall all {len(steps)}? [Y/n] ").strip().lower()
    except EOFError:
        return checks
    if answer not in ("", "y", "yes"):
        print("  skipped")
        return checks
    print()
    # shell=True: the script is built from commands we authored (see
    # doctor.install_plan); stdio is inherited so sudo can ask for a password.
    proc = subprocess.run(plan["script"], shell=True)
    if proc.returncode != 0:
        print(f"  install script exited {proc.returncode}")
    try:
        fresh = doctor.run_checks()
    except Exception:  # noqa: BLE001 — a broken re-probe keeps the old list
        return checks
    done = {st["id"] for st in steps}
    changed = [c for c in fresh if c.id in done or c.pkg in plan["packages"]]
    if changed:
        print()
        print_checks(changed)
    return fresh


def _fix_checks(checks: list[Check]) -> list[Check]:
    """Interactive `doctor --fix`.

    First everything installable at once (:func:`_install_all`), then — one at
    a time, because each is interactive in its own way — the remaining fix
    commands (logins like `codex login` / `gh auth login`): ask, run with
    inherited stdio, re-probe just that check. Returns the checks list with
    re-probed results swapped in."""
    from backend import doctor

    def _rest(cs: list[Check]) -> list[Check]:
        return [
            c for c in cs if c.status in ("warn", "fail") and c.cmd and not c.install
        ]

    if not doctor.install_plan(checks)["steps"] and not _rest(checks):
        return checks
    if not sys.stdin.isatty():
        print()
        print(
            "--fix needs an interactive terminal to confirm; "
            "run `mindflock doctor --fix` yourself, or paste the fix lines above."
        )
        return checks
    checks = _install_all(list(checks))
    fixable = _rest(checks)
    if not fixable:
        return checks
    print()
    print(
        f"{len(fixable)} more fixable — I can run each command for you (Enter = yes)."
    )
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
        if fresh is None:
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
    # The account/agent combination has no verified route, so the session is
    # about to run on the CLI's own login. Loud here, because --account looks
    # like it worked otherwise.
    note = str((created or {}).get("note") or "")
    if note:
        print("  warning: %s" % note, file=sys.stderr)
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


def _quote_title(title: str) -> str:
    """A title as one URL path segment (titles may contain spaces)."""
    return urllib.parse.quote(title, safe=":@")


def _cmd_msg(args: argparse.Namespace) -> int:
    """POST /api/instances/{title}/messages from outside the flock (``from: ""``)."""
    text = sys.stdin.read() if args.text == ["-"] else " ".join(args.text)
    text = text.strip()
    if not text:
        print("error: empty message", file=sys.stderr)
        return 1
    base = client.discover(args.host, args.port)
    inst = _resolve_title(client.get(base, "/api/instances") or [], args.title)
    title = str(inst.get("title", ""))
    result = client.post(
        base,
        "/api/instances/%s/messages" % _quote_title(title),
        {"text": text, "from": "", "delivery": args.delivery},
    )
    result = result if isinstance(result, dict) else {}
    message = result.get("message")
    message = message if isinstance(message, dict) else {}
    outcome = str(result.get("delivery") or message.get("state") or "sent")
    print("sent %s to %s (%s)" % (message.get("id") or "message", title, outcome))
    if result.get("detail"):
        print("  note: %s" % result["detail"], file=sys.stderr)
    return 0


def _cmd_inbox(args: argparse.Namespace) -> int:
    """GET /api/instances/{title}/messages — reads WITHOUT marking anything read,
    so a peek from the terminal never steals a message from the agent."""
    base = client.discover(args.host, args.port)
    inst = _resolve_title(client.get(base, "/api/instances") or [], args.title)
    title = str(inst.get("title", ""))
    query = (
        "unread=0&include_consumed=1"
        if args.include_consumed
        else "unread=1&include_consumed=0"
    )
    result = client.get(
        base,
        "/api/instances/%s/messages?%s&mark_read=0&limit=200"
        % (_quote_title(title), query),
    )
    result = result if isinstance(result, dict) else {}
    if args.as_json:
        print(json.dumps(result, indent=2))
        return 0
    messages = [m for m in result.get("messages") or [] if isinstance(m, dict)]
    if not messages:
        print(
            "no %smessages for %s" % ("" if args.include_consumed else "unread ", title)
        )
        return 0
    for m in messages:
        print(_format_message(m))
    return 0


def _format_message(m: dict) -> str:
    """One line per message: `HH:MM:SS id [state] from sender: text`."""
    ts = m.get("ts")
    clock = (
        time.strftime("%H:%M:%S", time.localtime(ts))
        if isinstance(ts, (int, float))
        else "--:--:--"
    )
    sender = str(m.get("from") or "") or "(outside the flock)"
    kind = "result " if m.get("kind") == "result" else ""
    text = " ".join(str(m.get("text") or "").split())
    if len(text) > 200:
        text = text[:199] + "…"
    return "%s  %s  [%s] %sfrom %s: %s" % (
        clock,
        m.get("id", "?"),
        m.get("state", "?"),
        kind,
        sender,
        text,
    )


def _mcp_launch() -> Tuple[str, List[str], Dict[str, str]]:
    """(python, args, env) that start the MCP server from THIS install.

    ``-P`` keeps the client's cwd off ``sys.path`` (it may hold a different
    ``backend/`` — a MindFlock checkout, say); ``PYTHONPATH`` is added only when
    this package is not importable from the interpreter's own site-packages
    (a source checkout run through ``uv run``)."""
    import sysconfig

    import backend

    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(backend.__file__)))
    paths = sysconfig.get_paths()
    site_dirs = {
        os.path.realpath(p) for p in (paths.get("purelib"), paths.get("platlib")) if p
    }
    env: Dict[str, str] = {}
    if os.path.realpath(pkg_root) not in site_dirs:
        env["PYTHONPATH"] = pkg_root
    return sys.executable, ["-P", "-m", "backend.mcp"], env


def _mcp_config_text(
    scope: Optional[str], host: Optional[str], port: Optional[int]
) -> str:
    """The registration snippets ``mindflock mcp --print-config`` prints: a
    ``claude mcp add`` line, the same entry as JSON, and a Codex TOML table
    (timeouts sized for the 1500 s waits)."""
    import shlex

    python, args, env = _mcp_launch()
    if scope:
        env["MINDFLOCK_MCP_SCOPE"] = scope
    if host:
        env["MINDFLOCK_HOST"] = host
    if port is not None:
        env["MINDFLOCK_PORT"] = str(port)
    entry: Dict[str, object] = {"type": "stdio", "command": python, "args": args}
    if env:
        entry["env"] = env
    snippet = json.dumps({"mcpServers": {"mindflock": entry}}, indent=2)
    # The server name goes FIRST: `--env` is variadic in `claude mcp add` and
    # would swallow a name placed after it ("Invalid environment variable
    # format: mindflock" — verified against Claude Code 2.1.289).
    add = ["claude", "mcp", "add", "mindflock", "--scope", "user"]
    for key, value in env.items():
        add += ["--env", "%s=%s" % (key, value)]
    add += ["--", python, *args]
    # Every TOML string via json.dumps: a JSON string literal is a valid TOML
    # basic string (same escapes), so odd paths can't break the table.
    toml = [
        "[mcp_servers.mindflock]",
        "command = %s" % json.dumps(python),
        "args = [%s]" % ", ".join(json.dumps(a) for a in args),
        'env_vars = ["TMUX", "TMUX_PANE", "TMUX_TMPDIR", "MINDFLOCK_AUTH_TOKEN"]',
        "startup_timeout_sec = 30",
        "tool_timeout_sec = 1620",
    ]
    if env:
        toml += ["", "[mcp_servers.mindflock.env]"]
        toml += ["%s = %s" % (k, json.dumps(v)) for k, v in env.items()]
    lines = [
        "# MindFlock MCP server: register it with your own agent CLI.",
        "# (Sessions MindFlock starts with Claude or Codex get it automatically.)",
        "",
        "# Claude Code (user scope):",
        " ".join(shlex.quote(a) for a in add),
        "",
        "# ...or the same entry as JSON (.mcp.json / --mcp-config):",
        snippet,
        "",
        "# Codex (~/.codex/config.toml):",
        *toml,
    ]
    return "\n".join(lines) + "\n"


def _cmd_mcp(args: argparse.Namespace) -> int:
    """``mindflock mcp``: serve MCP on stdio, or print the registration config."""
    if args.print_config:
        sys.stdout.write(_mcp_config_text(args.scope, args.host, args.port))
        return 0
    from backend import mcp as mcp_server

    argv: List[str] = []
    if args.scope:
        argv += ["--scope", args.scope]
    if args.host:
        argv += ["--host", args.host]
    if args.port is not None:
        argv += ["--port", str(args.port)]
    return mcp_server.main(argv, prog="mindflock mcp")


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
    if plan.run_files:
        print("  MCP run files:        %d" % len(plan.run_files))
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
    if pid == "default":
        # Reserved: the AMBIENT_ID sentinel meaning "the CLI's own login" —
        # a profile so named would resolve to no overlay at all.
        print(
            "error: 'default' is reserved (it means the CLI's own login) — "
            "pick another id",
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


def _peer_link_id(base: str, needle: str) -> str:
    """A link id from an exact id or a unique prefix."""
    status = client.get(base, "/api/peer") or {}
    ids = [str(l.get("link_id") or "") for l in status.get("links") or []]
    if needle in ids:
        return needle
    hits = [i for i in ids if needle and i.startswith(needle)]
    if len(hits) == 1:
        return hits[0]
    raise client.ClientError(
        "no link matches %r" % needle if not hits else "%r is ambiguous" % needle
    )


def _format_link(link: dict) -> str:
    state = "connected" if link.get("connected") else "offline"
    perms = ",".join(k for k, v in (link.get("perms") or {}).items() if v) or "none"
    line = "%s  %-20s  %-8s  %-9s  SAS %s  peer may: %s" % (
        str(link.get("link_id") or "")[:12],
        link.get("peer_name") or "peer",
        link.get("role") or "",
        state,
        link.get("sas") or "?",
        perms,
    )
    if link.get("session_title"):
        line += "\n    shared session: %s" % link["session_title"]
    return line


def _cmd_peer(args: argparse.Namespace) -> int:
    """``mindflock peer …`` — thin client over ``/api/peer``."""
    base = client.discover(args.host, args.port)
    cmd = args.peer_command or "status"
    if cmd == "status":
        st = client.get(base, "/api/peer") or {}
        sb = st.get("sandbox") or {}
        ls = st.get("listen") or {}
        print(
            "peer links: %s"
            % ("on" if st.get("enabled") else "off (Settings → Peer links)")
        )
        print("name:       %s" % (st.get("display_name") or ""))
        if st.get("fingerprint"):
            print("identity:   %s" % st["fingerprint"])
        print(
            "sandbox:    %s"
            % ("ok" if sb.get("available") else "unavailable — %s" % sb.get("reason"))
        )
        print(
            "listener:   %s:%s (%s)"
            % (
                ls.get("host"),
                ls.get("port"),
                "listening" if ls.get("listening") else "stopped",
            )
        )
        rl = st.get("relay") or {}
        if rl.get("mode") and rl.get("mode") != "off":
            state = "up" if rl.get("running") else "down"
            if rl.get("error"):
                state += " — %s" % rl["error"]
            print("relay:      %s (%s)" % (rl.get("mode"), state))
            if rl.get("address"):
                print("relay addr: %s" % rl["address"])
        for inv in st.get("invites") or []:
            print(
                "invite:     %s (expires in %ss)"
                % (inv.get("invite_id"), inv.get("expires_in"))
            )
        for link in st.get("links") or []:
            print(_format_link(link))
        return 0
    if cmd == "links":
        links = (client.get(base, "/api/peer") or {}).get("links") or []
        if not links:
            print("no peer links")
        for link in links:
            print(_format_link(link))
        return 0
    if cmd == "invite":
        body: dict = {}
        if args.ttl is not None:
            body["ttl_s"] = args.ttl
        if args.advertise:
            body["advertise_host"] = args.advertise
        inv = client.post(base, "/api/peer/invites", body, timeout=90.0) or {}
        print(inv.get("code") or "")
        if inv.get("relay"):
            print(
                "Give this code to your peer (valid %ss, single use). They connect "
                "through the relay at %s — nothing to set up on their side."
                % (inv.get("expires_in"), inv.get("host")),
                file=sys.stderr,
            )
        else:
            print(
                "Give this code to your peer (valid %ss, single use). They must "
                "reach %s:%s — Tailscale recommended."
                % (inv.get("expires_in"), inv.get("host"), inv.get("port")),
                file=sys.stderr,
            )
        return 0
    if cmd == "join":
        # A relay code may wait up to ~75 s for a new tunnel's name to resolve.
        link = (
            client.post(base, "/api/peer/join", {"code": args.code}, timeout=120.0)
            or {}
        )
        print("paired with %s" % (link.get("peer_name") or "peer"))
        print(
            "SAS %s — compare it with your peer (voice/chat); if it differs, "
            "unlink now." % (link.get("sas") or "?")
        )
        return 0
    link_id = _peer_link_id(base, args.link)
    path = "/api/peer/links/%s" % urllib.parse.quote(link_id, safe="")
    if cmd == "unlink":
        client.delete(base, path + ("?delete_files=1" if args.delete_files else ""))
        print("unlinked %s" % link_id[:12])
        return 0
    if cmd == "share":
        body = {"repo_path": os.path.abspath(os.path.expanduser(args.repo))}
        if args.branch:
            body["branch"] = args.branch
        if args.program:
            body["program"] = args.program
        res = client.post(base, path + "/share", body, timeout=300.0) or {}
        sess = res.get("session") or {}
        print("sharing; sandboxed session: %s" % (sess.get("title") or "?"))
        return 0
    if cmd == "unshare":
        res = (
            client.delete(
                base, path + "/share" + ("?delete_files=1" if args.delete_files else "")
            )
            or {}
        )
        if res.get("deleted"):
            print("unshared; folder deleted")
        else:
            print("unshared; folder kept at %s" % (res.get("folder") or "?"))
        return 0
    if cmd == "export":
        res = (
            client.post(
                base,
                path + "/export",
                {
                    "target_repo": os.path.abspath(
                        os.path.expanduser(args.target_repo)
                    ),
                    "branch_name": args.branch,
                },
                timeout=300.0,
            )
            or {}
        )
        print("exported to %s in %s" % (args.branch, args.target_repo))
        if res.get("sha"):
            print("  %s" % res["sha"])
        return 0
    if cmd == "address":
        res = client.post(base, path + "/address", {"address": args.address}) or {}
        print("link %s now dials %s" % (link_id[:12], res.get("peer_addr") or "?"))
        return 0
    print("error: unknown peer command %r" % cmd, file=sys.stderr)
    return 2


#: ``devices join DEVICE`` (no code) polls the request this often. The server
#: drops a request after 10 minutes, so the client gives up a little later —
#: by then the server has already said "expired".
_JOIN_POLL_S = 2.0
_JOIN_WAIT_S = 660.0

#: Join states that end the wait (``waiting``/``joining`` keep it going).
#: ``idle`` means the request vanished — cancelled from the web UI, or the
#: server restarted (requests live in memory only).
_JOIN_DONE = ("joined", "denied", "expired", "error", "idle")


def _time_left(ts: object) -> str:
    """``9m`` / ``40s`` until an epoch timestamp (``0s`` once past)."""
    try:
        left = max(0, int(float(ts) - time.time()))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
    return "%dm" % (left // 60) if left >= 60 else "%ds" % left


def _confirm(question: str) -> bool:
    """A ``[y/N]`` prompt; EOF/Ctrl-C (a script, a closed stdin) is a no."""
    try:
        answer = input(question + " [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return False
    return answer.strip().lower() in ("y", "yes")


def _fleet_status(base: str) -> dict:
    return client.get(base, "/api/fleet") or {}


def _match_device(rows: List[dict], needle: str, key_field: str) -> Optional[dict]:
    """The row whose device key is ``needle``, else the ONE whose host is
    (case-insensitive) — people type what the UI shows, which is the host."""
    for row in rows:
        if str(row.get(key_field) or "") == needle:
            return row
    low = needle.lower()
    hits = [r for r in rows if str(r.get("host") or "").lower() == low]
    if len(hits) > 1:
        raise client.ClientError(
            "%r names %d devices — use the device name: %s"
            % (needle, len(hits), ", ".join(str(r.get(key_field)) for r in hits))
        )
    return hits[0] if hits else None


def _pending_request(status: dict, needle: str) -> dict:
    """A pending join request from its id, device name, host, or a unique id
    prefix — so ``devices approve laptop`` works without copying an id."""
    reqs = [r for r in status.get("requests") or [] if isinstance(r, dict)]
    for r in reqs:
        if str(r.get("id") or "") == needle:
            return r
    hit = _match_device(reqs, needle, "device")
    if hit is not None:
        return hit
    pref = [r for r in reqs if needle and str(r.get("id") or "").startswith(needle)]
    if len(pref) == 1:
        return pref[0]
    if not reqs:
        raise client.ClientError("no device is asking to join right now")
    raise client.ClientError(
        "no pending request matches %r (waiting: %s)"
        % (
            needle,
            ", ".join(str(r.get("device") or r.get("id")) for r in reqs),
        )
    )


def _print_devices(st: dict) -> None:
    """The human ``devices list`` view of the GET /api/fleet payload."""
    me = st.get("self") or {}
    me_key = str(me.get("key") or "")
    if st.get("in_fleet"):
        members = [m for m in st.get("members") or [] if isinstance(m, dict)]
        print("Your devices (%d):" % len(members))
        for m in members:
            if m.get("self"):
                glyph, state = "✓", "this device"
            elif not m.get("reachable"):
                glyph, state = "-", "offline"
            elif m.get("same_fleet") is False:
                # Reachable but its hello names another (or no) group: it left
                # or never got the roster. Sync skips it until it re-joins.
                glyph, state = "!", "reachable, but not in this group any more"
            else:
                glyph, state = "✓", "reachable"
            if m.get("version") and not m.get("self"):
                state += " · v%s" % m["version"]
            if m.get("error"):
                state += " — %s" % m["error"]
            if m.get("automation"):
                state += " · runs PR review & issues"
            print("  %s %-20s %s" % (glyph, m.get("host") or m.get("key"), state))
        _print_automation_hint(members)
        if st.get("stale_key"):
            print(
                "! this device's key is out of date (it was removed or re-keyed while "
                "away) — re-join: mindflock devices join DEVICE"
            )
    else:
        print("This computer isn't joined with your other devices yet.")
    if st.get("gate_warning"):
        print(
            "! the access-token gate is off while serving beyond localhost — "
            "turn it on (Settings → Security) before joining devices"
        )
    for r in st.get("requests") or []:
        print(
            "Asking to join: %s · code %s — check %s shows the same code, then: "
            "mindflock devices approve %s"
            % (
                r.get("host") or r.get("device"),
                r.get("code") or "?",
                r.get("host") or r.get("device"),
                r.get("device") or r.get("id"),
            )
        )
    for inv in st.get("invites") or []:
        print(
            "Code %s (expires in %s) — on the new computer: %s"
            % (
                inv.get("code"),
                _time_left(inv.get("expires_at")),
                inv.get("command")
                or "mindflock devices join %s %s" % (me_key, inv.get("code")),
            )
        )
    join = st.get("join") or {}
    if join.get("state") == "waiting":
        print(
            "Waiting for approval on %s — code %s (stop asking: mindflock devices "
            "cancel)"
            % (join.get("host") or join.get("device"), join.get("code") or "?")
        )
    elif join.get("state") == "joining":
        print("Joining %s…" % (join.get("host") or join.get("device")))
    others = [
        c
        for c in st.get("candidates") or []
        if isinstance(c, dict) and c.get("reachable") and not c.get("member")
    ]
    if others:
        print("Other computers on your tailnet:")
        for c in others:
            name = c.get("host") or c.get("device")
            if not int(c.get("fleet_proto") or 0):
                hint = "update MindFlock on %s first" % name
            elif c.get("has_token"):
                hint = "mindflock devices add %s" % c.get("device")
            else:
                hint = "mindflock devices join %s" % c.get("device")
            print("  %-20s %s" % (name, hint))
    if not st.get("in_fleet") and not others:
        print(
            "Make a code here with `mindflock devices add`, or run MindFlock on your "
            "other computer (same tailnet) and `mindflock devices join` it."
        )


def _print_automation_hint(members: List[dict]) -> None:
    """Warn when no member — or more than one — runs PR review and issue
    handling: both halves act on GitHub, so two devices doing it review every
    PR twice, and none means nobody does. Older servers send no
    ``automation`` field at all, and a member too old to say comes as null:
    leave those out (say nothing when fewer than two are known)."""
    members = [m for m in members if isinstance(m.get("automation"), bool)]
    if len(members) < 2:
        return
    on = [str(m.get("host") or m.get("key")) for m in members if m.get("automation")]
    if not on:
        print(
            "! none of your devices runs PR review & issue handling — turn it on "
            "for one: Settings → Devices → Run PR review and issue handling here"
        )
    elif len(on) > 1:
        print(
            "! %s all run PR review & issue handling — each PR gets reviewed more "
            "than once; turn it off on all but one (Settings → Devices)" % ", ".join(on)
        )


def _sync_error(res: object) -> None:
    """Print the admission's ``sync_error`` (settings sync failed to start
    here) — the membership stands either way, so the exit code doesn't
    change."""
    err = res.get("sync_error") if isinstance(res, dict) else ""
    if err:
        print("! settings sync: %s" % err, file=sys.stderr)


def _withdraw_join(base: str) -> dict:
    """``DELETE /api/fleet/request``: stop asking to join — the server also
    withdraws the request on the device that was asked. It refuses (answers
    ``joining``) once that device has approved and the join is under way."""
    return client.delete(base, "/api/fleet/request", timeout=30.0) or {}


def _report_join(st: dict) -> int:
    """Print how a join ended (a ``join_status()`` dict); exit code."""
    state = st.get("state")
    host = st.get("host") or st.get("device") or "the other device"
    if state == "joined":
        print("joined — this computer is now one of your devices, with %s" % host)
        err = st.get("sync_error") or st.get("error")
        if err:
            # The join stands; only turning settings sync on failed.
            print("! settings sync: %s" % err, file=sys.stderr)
        return 0
    if state == "denied":
        print("%s said no" % host, file=sys.stderr)
    elif state == "expired":
        print(
            "the request expired before %s approved it — run the command again" % host,
            file=sys.stderr,
        )
    elif state == "idle":
        print("the join request was cancelled", file=sys.stderr)
    else:
        print("error: %s" % (st.get("error") or "join failed"), file=sys.stderr)
    return 1


def _wait_for_join(base: str, host: str) -> int:
    """Poll our join request until it ends. Ctrl-C (and giving up) withdraws
    it — on the asked device too, so a late approval can't add a member
    that never collects its key."""
    deadline = time.monotonic() + _JOIN_WAIT_S
    try:
        while time.monotonic() < deadline:
            time.sleep(_JOIN_POLL_S)
            try:
                res = client.get(base, "/api/fleet/request") or {}
            except client.ServerNotFound:
                # A blip (or a restart) of our own server: keep waiting;
                # a restart loses the request and reads back as "idle".
                continue
            if res.get("state") in _JOIN_DONE:
                return _report_join(res)
    except KeyboardInterrupt:
        res = _withdraw_join(base)
        if res.get("state") == "joining":
            print(
                "\n%s already approved — the join is finishing; check it with "
                "`mindflock devices`" % host,
                file=sys.stderr,
            )
        else:
            print("\ncancelled — stopped asking %s" % host, file=sys.stderr)
        return 130
    if _withdraw_join(base).get("state") == "joining":
        print(
            "%s approved just now — the join is finishing; check it with "
            "`mindflock devices`" % host,
            file=sys.stderr,
        )
        return 1
    print("gave up waiting for %s to approve" % host, file=sys.stderr)
    return 1


def _cmd_devices(args: argparse.Namespace) -> int:
    """``mindflock devices …`` — thin client over ``/api/fleet`` (Settings →
    Devices in the UI). The server does the device-to-device talking; the CLI
    only ever reaches its own server."""
    base = client.discover(args.host, args.port)
    cmd = args.devices_command or "list"
    if cmd == "list":
        st = _fleet_status(base)
        if getattr(args, "as_json", False):
            print(json.dumps(st, indent=2))
        else:
            _print_devices(st)
        return 0
    if cmd == "add":
        if args.device:
            st = _fleet_status(base)
            cand = _match_device(
                [c for c in st.get("candidates") or [] if isinstance(c, dict)],
                args.device,
                "device",
            )
            device = str(cand.get("device")) if cand else args.device
            # Talks to the other device and adopts it there: allow for its
            # settings-sync kick-off, not just one round-trip.
            res = client.post(
                base, "/api/fleet/add-paired", {"device": device}, timeout=60.0
            )
            print("added %s to your devices" % ((cand or {}).get("host") or device))
            _sync_error(res)
            return 0
        inv = client.post(base, "/api/fleet/invite") or {}
        code = str(inv.get("code") or "")
        # The code alone on stdout (scriptable); the instructions on stderr.
        print(code)
        print(
            "On the other computer run:  %s\n(or there: Settings → Devices → "
            "choose %s → Enter code). Single use, expires in %s."
            % (
                inv.get("command")
                or "mindflock devices join %s %s" % (inv.get("device") or "?", code),
                inv.get("device") or "this device",
                _time_left(inv.get("expires_at")),
            ),
            file=sys.stderr,
        )
        return 0
    if cmd == "join":
        st = _fleet_status(base)
        cand = _match_device(
            [c for c in st.get("candidates") or [] if isinstance(c, dict)],
            args.device,
            "device",
        )
        device = str(cand.get("device")) if cand else args.device
        code = " ".join(args.code).strip()
        host = (cand or {}).get("host") or device
        pending = st.get("join") or {}
        if pending.get("state") in ("waiting", "joining"):
            asked = {str(pending.get(k) or "").lower() for k in ("device", "host")}
            same = bool({device.lower(), args.device.lower()} & (asked - {""}))
            if code or not same:
                raise client.ClientError(
                    "already asking %s to join — stop that first: mindflock devices "
                    "cancel" % (pending.get("host") or pending.get("device"))
                )
            # The same request is still out (this command was interrupted, or
            # it was made in the app): wait for it instead of failing.
            print(
                "Still waiting for %s; check it shows code %s  (Ctrl-C to stop asking)"
                % (pending.get("host") or host, pending.get("code") or "?")
            )
            return _wait_for_join(base, pending.get("host") or host)
        if not args.yes and not _confirm(
            "This computer takes %s's shared settings where %s has them; your own "
            "stay where it has none. Join %s's devices?" % (host, host, host)
        ):
            print("not joined")
            return 1
        if code:
            # Redeem + adopt + first settings pull happen server-side.
            res = (
                client.post(
                    base,
                    "/api/fleet/join",
                    {"device": device, "code": code},
                    timeout=60.0,
                )
                or {}
            )
            return _report_join(res)
        res = (
            client.post(base, "/api/fleet/request", {"device": device}, timeout=30.0)
            or {}
        )
        if res.get("state") in _JOIN_DONE:
            return _report_join(res)
        host = res.get("host") or host
        print(
            "Approve on %s; check it shows code %s  (Ctrl-C to stop asking)"
            % (host, res.get("code") or "?")
        )
        return _wait_for_join(base, host)
    if cmd == "cancel":
        before = (_fleet_status(base).get("join") or {}).get("state")
        if before not in ("waiting", "joining"):
            print("not asking to join anything")
            return 0
        res = _withdraw_join(base)
        if res.get("state") == "joining":
            print(
                "too late to cancel — %s already approved; the join is finishing"
                % (res.get("host") or res.get("device") or "the other device"),
                file=sys.stderr,
            )
            return 1
        print("stopped asking to join")
        return 0
    if cmd in ("approve", "deny"):
        req = _pending_request(_fleet_status(base), args.request)
        who = req.get("host") or req.get("device")
        if cmd == "approve" and not args.yes:
            # The code is the whole defence against approving a stranger who
            # asked at the same moment — make the person look at it.
            if not _confirm(
                "%s wants to join your devices — code %s. Does %s show the same "
                "code? Approve?" % (who, req.get("code") or "?", who)
            ):
                print(
                    "not approved (deny it with `mindflock devices deny %s`)"
                    % (req.get("device") or req.get("id"))
                )
                return 1
        path = "/api/fleet/requests/%s/%s" % (
            urllib.parse.quote(str(req.get("id") or ""), safe=""),
            cmd,
        )
        # Approving also starts settings sync here (after_admit): allow for
        # that, not just one round-trip.
        res = client.post(base, path, timeout=60.0)
        print(
            "approved %s — it joins your devices now" % who
            if cmd == "approve"
            else "denied %s" % who
        )
        _sync_error(res)
        return 0
    if cmd == "remove":
        st = _fleet_status(base)
        members = [m for m in st.get("members") or [] if isinstance(m, dict)]
        m = _match_device(members, args.device, "key")
        if m is None:
            raise client.ClientError(
                "%r isn't one of your devices (%s)"
                % (args.device, ", ".join(str(x.get("key")) for x in members) or "none")
            )
        who = m.get("host") or m.get("key")
        rotate = not args.keep_tokens
        if not args.yes and not _confirm(
            "Remove %s from your devices? It stops getting settings and ticket "
            "claims, your other devices get a new shared key, and %s"
            % (
                who,
                (
                    "every device's own access token is replaced (phones and "
                    "other places holding one need the new token)."
                    if rotate
                    else "access tokens it already holds KEEP working "
                    "(--keep-tokens)."
                ),
            )
        ):
            print("aborted")
            return 1
        res = (
            client.post(
                base,
                "/api/fleet/members/%s/remove"
                % urllib.parse.quote(str(m.get("key")), safe=""),
                {"rotate_tokens": rotate},
                timeout=60.0,
            )
            or {}
        )
        print("removed %s" % who)
        if res.get("rekeyed"):
            print("new key sent to: %s" % ", ".join(map(str, res["rekeyed"])))
        if res.get("missed"):
            print(
                "! couldn't reach %s — it must join again (mindflock devices join)"
                % ", ".join(map(str, res["missed"])),
                file=sys.stderr,
            )
        if res.get("rotated"):
            print("new access token on: %s" % ", ".join(map(str, res["rotated"])))
        if res.get("rotate_failed"):
            print(
                "! couldn't replace the access token on %s — do it there "
                "(Settings → Security)" % ", ".join(map(str, res["rotate_failed"])),
                file=sys.stderr,
            )
        return 0
    if cmd == "leave":
        if not args.yes and not _confirm(
            "Take this computer out of your devices? Settings stop syncing here."
        ):
            print("aborted")
            return 1
        client.post(base, "/api/fleet/leave", timeout=60.0)
        print("left your devices — settings sync is off on this computer")
        return 0
    print("error: unknown devices command %r" % cmd, file=sys.stderr)
    return 2


_SESSION_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "new": _cmd_new,
    "ls": _cmd_ls,
    "attach": _cmd_attach,
    "rm": _cmd_rm,
    "open": _cmd_open,
    "events": _cmd_events,
    "msg": _cmd_msg,
    "inbox": _cmd_inbox,
    "peer": _cmd_peer,
    "devices": _cmd_devices,
}


def main(argv: Optional[List[str]] = None) -> int:
    """Parse ``argv`` and dispatch to the matching subcommand; return the exit code.

    ``doctor``, ``mcp`` and the J1 session commands
    (new/ls/attach/rm/open/events/msg/inbox) run in-process; any other invocation — including no subcommand at all — falls
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
    if args.command == "mcp":
        # A stdio protocol server (or a config printer): it discovers the
        # server lazily per tool call, so it never goes through the
        # ServerNotFound handler below either.
        return _cmd_mcp(args)
    if args.command == "token":
        # Offline on purpose: it reads the same store the server does, so it
        # works when the only thing in the way is the sign-in page itself
        # (the desktop app runs this to sign in to its own server).
        return _cmd_token()
    if args.command == "accounts":
        # Not a session command either: it prefers a running server but falls
        # back to the local settings store, so ServerNotFound is a routing
        # decision here, not an error.
        try:
            return _cmd_accounts(args)
        except client.AuthRejected as err:
            # A server is up but refuses our token: editing the local store
            # behind its back is exactly what this routing exists to avoid.
            print("error: %s" % err, file=sys.stderr)
            return 1
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
