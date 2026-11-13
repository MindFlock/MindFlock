"""The tmux commands the session host answers, with tmux's output and
errors. Each ``cmd_<name>`` runs under the host lock with parsed
:class:`~backend.session.winmux.proto.Args`; a failure raises
:class:`~backend.session.winmux.proto.CmdError` with tmux's message (exit
status 1, and a ``;`` chain stops there).

One window with one pane per session — the shape every MindFlock session
has — so window and pane targets resolve to the session's only pane, and
``new-window`` / ``split-window`` are refused with a sentence.

Accepted and stored but without effect (documented, not silently wrong):
``bind-key`` / ``unbind-key`` (the bindings drive tmux's own client; the
browser and the console client handle the wheel and keys themselves),
``mouse``, ``status`` and every option the host does not act on.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import socket
import subprocess
import sys
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from backend import compat
from backend.session.winmux import formats, keys, proto, ptys
from backend.session.winmux.proto import Args, CmdError

if TYPE_CHECKING:  # pragma: no cover
    from backend.session.winmux.host import Client, Ctx, Pane, Session

__all__ = ["execute", "find_session", "parse_command_string"]


def execute(ctx: "Ctx", argv: List[str]) -> None:
    if not argv:
        return
    name = proto.canonical(argv[0])
    if name is None:
        raise CmdError("unknown command: %s" % argv[0])
    spec = proto.COMMANDS[name][1]
    try:
        args = Args(argv[1:], spec)
    except CmdError as e:
        raise CmdError("command %s: %s" % (name, e))
    fn = _TABLE.get(name)
    if fn is None:  # pragma: no cover - every COMMANDS entry has a function
        raise CmdError("unknown command: %s" % argv[0])
    try:
        fn(ctx, args)
    except CmdError:
        raise
    except Exception as e:  # noqa: BLE001 - a host bug must not kill the request
        import traceback

        traceback.print_exc()
        raise CmdError("winmux: %s failed: %s" % (name, e))


def parse_command_string(text: str) -> List[List[str]]:
    """A tmux command string (a hook's or binding's) → argv lists."""
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";")
        lex.whitespace_split = True
        lex.commenters = ""
        toks = list(lex)
    except ValueError:
        return []
    cmds, cur = [], []
    for t in toks:
        if t == ";":
            if cur:
                cmds.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        cmds.append(cur)
    return cmds


# -- targets ---------------------------------------------------------------------


def _current(ctx: "Ctx") -> Optional["Session"]:
    s = getattr(ctx, "hook_session", None)
    if s is not None and ctx.ns.sessions.get(s.name) is s:
        return s
    if ctx.client is not None:
        return ctx.client.session
    pane_id = ctx.env.get("TMUX_PANE", "")
    if pane_id:
        p = ctx.ns.pane_by_id(pane_id)
        if p is not None:
            return p.session
    if not ctx.ns.sessions:
        return None
    return max(ctx.ns.sessions.values(), key=lambda s: (s.activity, s.id))


def find_session(ctx: "Ctx", target: Optional[str], kind: str = "session") -> "Session":
    """tmux's target resolution, for one-window/one-pane sessions:
    ``name``, ``=name`` (exact), ``name:``, ``name:0.0``, ``$id``, ``%pane``,
    ``@window``, a unique prefix, then an fnmatch pattern."""
    ns = ctx.ns
    if target is None or target == "":
        s = _current(ctx)
        if s is None:
            raise CmdError("no current session")
        return s
    t = target
    if t.startswith("%"):
        p = ns.pane_by_id(t.split(":")[0].split(".")[0])
        if p is None:
            raise CmdError("can't find pane: %s" % t)
        return p.session
    if t.startswith("@"):
        for s in ns.sessions.values():
            if "@%d" % s.window_id == t:
                return s
        raise CmdError("can't find window: %s" % t)
    exact = t.startswith("=")
    if exact:
        t = t[1:]
    name, colon, rest = t.partition(":")
    if not colon and name not in ns.sessions and "." in name:
        name, _, rest = name.rpartition(".")
        rest = "." + rest
    if name == "":
        s = _current(ctx)
        if s is None:
            raise CmdError("no current session")
    elif name.startswith("$"):
        s = next((x for x in ns.sessions.values() if "$%d" % x.id == name), None)
    else:
        s = ns.sessions.get(name)
        if s is None and not exact:
            hits = [x for x in ns.sessions.values() if x.name.startswith(name)]
            if len(hits) == 1:
                s = hits[0]
            elif not hits:
                hits = [
                    x for x in ns.sessions.values() if fnmatch.fnmatchcase(x.name, name)
                ]
                s = hits[0] if len(hits) == 1 else None
    if s is None:
        if kind == "pane":
            raise CmdError("can't find pane: %s" % name)
        raise CmdError("can't find session: %s" % name)
    if rest:
        win, dot, pane = (
            rest.lstrip(".").partition(".")
            if not rest.startswith(".")
            else ("", ".", rest[1:])
        )
        if win not in (
            "",
            "0",
            "^",
            "$",
            "!",
            "+",
            "-",
            "{start}",
            "{end}",
            "{last}",
        ) and win not in (
            s.window_name,
            "@%d" % s.window_id,
        ):
            raise CmdError("can't find window: %s" % win)
        if dot and pane not in (
            "",
            "0",
            "{active}",
            "{last}",
            "%%%d" % (s.pane.id if s.pane else -1),
        ):
            raise CmdError("can't find pane: %s" % pane)
    return s


def find_pane(ctx: "Ctx", target: Optional[str]) -> Tuple["Session", "Pane"]:
    s = find_session(ctx, target, "pane")
    if s.pane is None or s.pane.gone:
        raise CmdError("can't find pane: %s" % (target or ""))
    return s, s.pane


# -- formats ------------------------------------------------------------------------


def lookup_for(
    ctx: "Ctx",
    session: Optional["Session"] = None,
    client: Optional["Client"] = None,
    extra: Optional[Dict[str, str]] = None,
) -> Callable[[str], Optional[str]]:
    host = ctx.host
    ns = ctx.ns
    pane = session.pane if session is not None else None
    if client is None and session is not None and ctx.client is not None:
        if ctx.client.session is session:
            client = ctx.client
    if client is None and session is not None and session.clients:
        client = max(session.clients, key=lambda c: c.activity)
    cache: Dict[str, str] = {}

    def fg_pid() -> Optional[int]:
        if "_fg" not in cache:
            fp = None
            if pane is not None and not pane.dead:
                try:
                    fp = pane.pty.foreground_pid()
                except Exception:  # noqa: BLE001
                    fp = None
            cache["_fg"] = str(fp or "")
        return int(cache["_fg"]) if cache["_fg"] else None

    def look(name: str) -> Optional[str]:
        if extra and name in extra:
            return extra[name]
        t = pane.term if pane is not None else None
        scr = t.screen if t is not None else None
        if session is not None:
            if name == "session_name":
                return session.name
            if name == "session_id":
                return "$%d" % session.id
            if name == "session_created":
                return str(int(session.created))
            if name == "session_activity":
                return str(int(session.activity))
            if name == "session_last_attached":
                return str(int(session.last_attached)) if session.last_attached else ""
            if name == "session_attached":
                return str(len(session.clients))
            if name == "session_many_attached":
                return "1" if len(session.clients) > 1 else "0"
            if name == "session_windows":
                return "1"
            if name in ("session_path", "pane_start_path"):
                return pane.start_path if pane is not None else ""
            if name in ("session_grouped", "session_group_size"):
                return "0" if name == "session_grouped" else ""
            if name == "session_group":
                return ""
            if name == "session_format":
                return "1"
            if name in ("window_index", "pane_index"):
                return "0"
            if name == "window_id":
                return "@%d" % session.window_id
            if name == "window_name":
                return session.window_name or (
                    ptys.process_name(fg_pid())
                    if pane is not None and not pane.dead
                    else ""
                )
            if name in ("window_active", "pane_active", "window_panes"):
                return "1"
            if name in ("window_flags",):
                return "*"
            if name in ("window_zoomed_flag", "pane_synchronized", "pane_input_off"):
                return "0"
            if name in (
                "pane_at_top",
                "pane_at_bottom",
                "pane_at_left",
                "pane_at_right",
            ):
                return "1"
        if pane is not None:
            if name == "pane_id":
                return "%%%d" % pane.id
            if name == "pane_pid":
                return str(pane.pty.pid)
            if name == "pane_tty":
                return ""
            if name == "pane_dead":
                return "1" if pane.dead else "0"
            if name == "pane_dead_status":
                return "" if pane.dead_status is None else str(pane.dead_status)
            if name == "pane_current_command":
                if pane.dead:
                    return ""
                return ptys.process_name(fg_pid())
            if name == "pane_current_path":
                if t.cwd:
                    return t.cwd
                return ptys.process_cwd(fg_pid()) or pane.start_path
            if name == "pane_start_command":
                return pane.start_cmd
            if name == "pane_title":
                return t.title or socket.gethostname()
            if name in ("pane_width", "window_width"):
                return str(t.cols)
            if name in ("pane_height", "window_height"):
                return str(t.rows)
            if name == "pane_in_mode":
                return "1" if pane.mode is not None else "0"
            if name == "pane_mode":
                return "copy-mode" if pane.mode is not None else ""
            if name == "scroll_position":
                return str(pane.mode.offset) if pane.mode is not None else ""
            if name == "history_size":
                return str(t.history_size)
            if name == "history_limit":
                return str(t.history_limit)
            if name == "history_bytes":
                return "0"
            if name == "alternate_on":
                return "1" if t.alternate_on else "0"
            if name in ("alternate_saved_x", "alternate_saved_y"):
                if scr.alt is None:
                    return "0"
                return str(scr.alt[1].x if name.endswith("x") else scr.alt[1].y)
            if name == "cursor_x":
                return str(min(scr.cursor.x, scr.columns - 1))
            if name == "cursor_y":
                return str(scr.cursor.y)
            if name == "cursor_flag":
                return "0" if scr.cursor.hidden else "1"
            if name == "insert_flag":
                return "1" if (4 in scr.mode) else "0"
            if name == "keypad_cursor_flag":
                return "1" if t.app_cursor else "0"
            if name == "keypad_flag":
                return "1" if t.keypad else "0"
            if name == "wrap_flag":
                return "1" if ((7 << 5) in scr.mode) else "0"
            if name == "origin_flag":
                return "1" if ((6 << 5) in scr.mode) else "0"
            if name == "mouse_any_flag":
                return "1" if t.mouse_any else "0"
            if name == "mouse_standard_flag":
                return "1" if t.mode(1000) else "0"
            if name == "mouse_button_flag":
                return "1" if t.mode(1002) else "0"
            if name == "mouse_all_flag":
                return "1" if t.mode(1003) else "0"
            if name == "mouse_sgr_flag":
                return "1" if t.mode(1006) else "0"
            if name == "mouse_utf8_flag":
                return "1" if t.mode(1005) else "0"
            if name == "scroll_region_upper":
                return str(scr.margins.top if scr.margins else 0)
            if name == "scroll_region_lower":
                return str(scr.margins.bottom if scr.margins else scr.lines - 1)
        if client is not None:
            if name == "client_pid":
                return str(client.pid)
            if name == "client_activity":
                return str(int(client.activity))
            if name == "client_created":
                return str(int(client.created))
            if name in ("client_name", "client_tty"):
                return client.name
            if name == "client_width":
                return str(client.cols)
            if name == "client_height":
                return str(client.rows)
            if name == "client_readonly":
                return "1" if client.readonly else "0"
            if name == "client_session":
                return client.session.name
            if name == "client_termname":
                return client.termname
            if name == "client_flags":
                return "attached,focused" + (",read-only" if client.readonly else "")
        if name == "host":
            return socket.gethostname()
        if name == "host_short":
            return socket.gethostname().split(".")[0]
        if name == "pid":
            return str(os.getpid())
        if name == "version":
            return proto.VERSION
        if name == "socket_path":
            return proto.socket_label(ns.name)
        if name == "start_time":
            return str(int(host.started))
        if name == "server_sessions":
            return str(len(ns.sessions))
        if name.startswith("@"):
            if session is not None and name in session.options:
                return session.options[name]
            return ns.options.get(name)
        return None

    return look


def _fmt(ctx, fmt, session=None, client=None, extra=None) -> str:
    return formats.expand(fmt, lookup_for(ctx, session, client, extra))


# -- sessions ------------------------------------------------------------------------


def _sanitize(name: str) -> str:
    return name.replace(":", "_").replace(".", "_")


def _shell(ctx: "Ctx", session=None) -> str:
    sh = ctx.host.option(ctx.ns, session, "default-shell")
    if sh:
        return sh
    if compat.IS_WINDOWS:  # pragma: no cover - Windows
        return compat.posix_shell(bash=True)
    sh = ctx.ns.env.get("SHELL") or ""
    return sh if sh and os.path.exists(sh) else "/bin/sh"


#: The MindFlock server's own interpreter, as its launcher leaves it in the
#: environment (uv's Windows trampolines set ``PYTHONHOME`` /
#: ``UV_INTERNAL__PYTHONHOME``; a venv sets ``VIRTUAL_ENV``). A session's
#: python, claude and hooks must not run on the server's interpreter home,
#: so they do not reach a pane from the global environment — a session's
#: own (``new-session -e``, ``set-environment -t``) still does. (tmux on
#: Linux/macOS passes them through as before.)
SERVER_PYTHON_ENV = ("PYTHONHOME", "UV_INTERNAL__PYTHONHOME")


def _server_python_env(env: Dict[str, str]) -> List[str]:
    """The keys of ``env`` that name the server's own interpreter home:
    :data:`SERVER_PYTHON_ENV`, and ``VIRTUAL_ENV`` when it is the venv this
    host (started from the server's interpreter) runs in."""
    keys = [k for k in env if k.upper() in SERVER_PYTHON_ENV]
    for k in env:
        if k.upper() != "VIRTUAL_ENV":
            continue
        try:
            same = os.path.normcase(os.path.realpath(env[k])) == os.path.normcase(
                os.path.realpath(sys.prefix)
            )
        except (OSError, ValueError):
            same = False
        if same:
            keys.append(k)
    return keys


def _session_env(ctx: "Ctx", session: "Session") -> Dict[str, str]:
    env: Dict[str, str] = {k: v for k, v in ctx.ns.env.items() if v is not None}
    for k in _server_python_env(env):
        del env[k]
    upd = ctx.host.option(ctx.ns, session, "update-environment").split()
    for k in upd:
        if k in ctx.env:
            env[k] = ctx.env[k]
    for k, v in session.env.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


def cmd_new_session(ctx: "Ctx", a: Args) -> None:
    ns = ctx.ns
    if a.has("t"):
        raise CmdError("winmux: grouped sessions (new-session -t) are not supported")
    name = _sanitize(a.get("s") or "")
    if not name:
        n = 0
        while str(n) in ns.sessions:
            n += 1
        name = str(n)
    if name in ns.sessions:
        if a.has("A"):
            return  # -A: attach to the existing one (a non-console client: no-op)
        raise CmdError("duplicate session: %s" % name)
    host = ctx.host
    cwd = a.get("c")
    if cwd:
        cwd = proto.native_path(os.path.expanduser(_fmt(ctx, cwd)))
        if ctx.cwd and not os.path.isabs(cwd):
            cwd = os.path.join(ctx.cwd, cwd)
    else:
        cwd = ctx.cwd
    if not cwd or not os.path.isdir(cwd):
        cwd = ns.env.get("HOME") or ns.env.get("USERPROFILE") or os.path.expanduser("~")
    dsize = host.option(ns, None, "default-size")
    try:
        dcols, drows = (int(x) for x in dsize.split("x"))
    except ValueError:
        dcols, drows = 80, 24
    try:
        cols = int(a.get("x") or dcols) if a.get("x") != "-" else dcols
        rows = int(a.get("y") or drows) if a.get("y") != "-" else drows
    except ValueError:
        raise CmdError("width or height is invalid")
    from backend.session.winmux.host import Session

    session = Session(ns, name, host.next_id("session"), host.next_id("window"))
    if a.has("n"):
        session.window_name = a.get("n") or ""
    for item in a.all("e"):
        k, eq, v = item.partition("=")
        if not eq:
            raise CmdError("invalid environment: %s" % item)
        session.env[k] = v
    env = _session_env(ctx, session)
    if not a.pos:
        sh = _shell(ctx, session)
        base = os.path.basename(sh).lower().replace(".exe", "")
        argv = [sh, "-l"] if base in ("bash", "sh", "zsh", "dash", "ksh") else [sh]
        start = ""
    elif len(a.pos) == 1:
        argv = [_shell(ctx, session), "-c", a.pos[0]]
        start = a.pos[0]
    else:
        argv = list(a.pos)
        start = " ".join(shlex.quote(x) for x in a.pos)
    try:
        pane = host.spawn_pane(session, argv, cwd, env, rows, cols)
    except Exception as e:  # noqa: BLE001
        raise CmdError("create pane failed: %s" % e)
    pane.start_cmd = start
    ns.sessions[name] = session
    host.had_session = True
    if a.has("P"):
        ctx.print(_fmt(ctx, a.get("F") or "#{session_name}:", session))
    host.fire(ns, "session-created", session)


def cmd_has_session(ctx: "Ctx", a: Args) -> None:
    find_session(ctx, a.get("t"))


def cmd_kill_session(ctx: "Ctx", a: Args) -> None:
    s = find_session(ctx, a.get("t"))
    if a.has("C"):
        return  # clear alerts: there are none
    victims = [x for x in ctx.ns.sessions.values() if x is not s] if a.has("a") else [s]
    for v in victims:
        ctx.host.destroy_session(v, "[exited]\r\n")


def cmd_kill_pane(ctx: "Ctx", a: Args) -> None:
    s, _ = find_pane(ctx, a.get("t"))
    if not a.has("a"):  # -a: every OTHER pane — there is none
        ctx.host.destroy_session(s, "[exited]\r\n")


def cmd_kill_window(ctx: "Ctx", a: Args) -> None:
    cmd_kill_pane(ctx, a)


def cmd_kill_server(ctx: "Ctx", a: Args) -> None:
    for s in list(ctx.ns.sessions.values()):
        ctx.host.destroy_session(s, "[server exited]\r\n")


def cmd_start_server(ctx: "Ctx", a: Args) -> None:
    return


def cmd_rename_session(ctx: "Ctx", a: Args) -> None:
    s = find_session(ctx, a.get("t"))
    if not a.pos:
        raise CmdError("command rename-session: too few arguments (need at least 1)")
    new = _sanitize(a.pos[0])
    if not new:
        raise CmdError("invalid session: %s" % a.pos[0])
    if new == s.name:
        return
    if new in ctx.ns.sessions:
        raise CmdError("duplicate session: %s" % new)
    del ctx.ns.sessions[s.name]
    s.name = new
    ctx.ns.sessions[new] = s
    ctx.host.fire(ctx.ns, "session-renamed", s)


def cmd_rename_window(ctx: "Ctx", a: Args) -> None:
    s = find_session(ctx, a.get("t"))
    s.window_name = a.pos[0] if a.pos else ""


def cmd_select_pane(ctx: "Ctx", a: Args) -> None:
    s, pane = find_pane(ctx, a.get("t"))
    if a.has("T"):
        pane.term.title = a.get("T") or ""


def cmd_select_window(ctx: "Ctx", a: Args) -> None:
    find_session(ctx, a.get("t"))


def cmd_refresh_client(ctx: "Ctx", a: Args) -> None:
    return


def _sessions_sorted(ctx: "Ctx") -> List["Session"]:
    return sorted(ctx.ns.sessions.values(), key=lambda s: s.name)


def _filter(ctx, a: Args, session=None, client=None, extra=None) -> bool:
    f = a.get("f")
    return f is None or formats.truthy(_fmt(ctx, f, session, client, extra))


LS_FORMAT = (
    "#{session_name}: #{session_windows} windows (created #{t:session_created})"
    "#{?session_attached, (attached),}"
)


def cmd_list_sessions(ctx: "Ctx", a: Args) -> None:
    fmt = a.get("F") or LS_FORMAT
    for s in _sessions_sorted(ctx):
        if _filter(ctx, a, s):
            ctx.print(_fmt(ctx, fmt, s))


LSP_FORMAT = (
    "#{pane_index}: [#{pane_width}x#{pane_height}] [history "
    "#{history_size}/#{history_limit}, #{history_bytes} bytes] #{pane_id}"
    "#{?pane_active, (active),}#{?pane_dead, (dead),}"
)


def cmd_list_panes(ctx: "Ctx", a: Args) -> None:
    if a.has("a"):
        sessions, prefix = _sessions_sorted(ctx), "#{session_name}:#{window_index}."
    else:
        sessions = [find_session(ctx, a.get("t"))]
        prefix = "#{window_index}." if a.has("s") else ""
    fmt = a.get("F") or (prefix + LSP_FORMAT)
    for s in sessions:
        if s.pane is not None and _filter(ctx, a, s):
            ctx.print(_fmt(ctx, fmt, s))


def cmd_list_windows(ctx: "Ctx", a: Args) -> None:
    sessions = _sessions_sorted(ctx) if a.has("a") else [find_session(ctx, a.get("t"))]
    default = (
        "#{window_index}: #{window_name}#{window_flags} (#{window_panes} panes)"
        " [#{window_width}x#{window_height}]"
    )
    if a.has("a"):
        default = "#{session_name}:" + default
    for s in sessions:
        if _filter(ctx, a, s):
            ctx.print(_fmt(ctx, a.get("F") or default, s))


LSC_FORMAT = (
    "#{client_name}: #{session_name} [#{client_width}x#{client_height} "
    "#{client_termname}]#{?client_readonly, (ro),}"
)


def cmd_list_clients(ctx: "Ctx", a: Args) -> None:
    sessions = [find_session(ctx, a.get("t"))] if a.has("t") else _sessions_sorted(ctx)
    fmt = a.get("F") or LSC_FORMAT
    for s in sessions:
        for c in list(s.clients):
            if _filter(ctx, a, s, c):
                ctx.print(_fmt(ctx, fmt, s, c))


DISPLAY_FORMAT = (
    "[#{session_name}] #{window_index}:#{window_name}, current pane "
    "#{pane_index} - (#{t:start_time})"
)

_ALL_VARS = (
    "session_name session_id session_created session_attached session_windows "
    "window_index window_id window_width window_height pane_index pane_id "
    "pane_pid pane_current_command pane_current_path pane_width pane_height "
    "pane_dead pane_in_mode alternate_on cursor_x cursor_y history_size "
    "history_limit mouse_any_flag mouse_sgr_flag scroll_position"
).split()


def cmd_display_message(ctx: "Ctx", a: Args) -> None:
    s = find_session(ctx, a.get("t"), "pane")
    client = None
    if a.has("c"):
        name = a.get("c")
        client = next((c for c in s.clients if c.name == name), None)
        if client is None:
            raise CmdError("can't find client: %s" % name)
    if a.has("a"):
        look = lookup_for(ctx, s, client)
        for v in _ALL_VARS:
            ctx.print("%s=%s" % (v, look(v) or ""))
        return
    msg = a.get("F") if a.has("F") else (" ".join(a.pos) if a.pos else DISPLAY_FORMAT)
    if a.has("I"):
        if s.pane is not None:
            s.pane.write(ctx.stdin)
        return
    text = msg if a.has("l") else _fmt(ctx, msg, s, client)
    if a.has("p"):
        ctx.print(text)


# -- capture / keys --------------------------------------------------------------------


def _set_buffer(ctx: "Ctx", name: Optional[str], data: bytes) -> str:
    from backend.session.winmux.host import Buffer

    ns = ctx.ns
    auto = not name
    if auto:
        name = "buffer%04d" % ns.buffer_seq
        ns.buffer_seq += 1
    ns.buffers[name] = Buffer(name, data, auto)
    ns.buffers.move_to_end(name)
    try:
        limit = int(ctx.host.option(ns, None, "buffer-limit"))
    except ValueError:
        limit = 50
    autos = [b for b in ns.buffers.values() if b.auto]
    while len(autos) > limit:
        del ns.buffers[autos.pop(0).name]
    return name


def cmd_capture_pane(ctx: "Ctx", a: Args) -> None:
    s, pane = find_pane(ctx, a.get("t"))
    for opt, label in (("S", "start"), ("E", "end")):
        v = a.get(opt)
        if v is not None and v != "-" and not re.match(r"^-?\d+$", v):
            raise CmdError("%s line invalid" % label)
    try:
        text = pane.term.capture(
            start=a.get("S"),
            end=a.get("E"),
            codes=a.has("e"),
            join=a.has("J"),
            keep_spaces=a.has("N"),
            alternate=a.has("a"),
        )
    except ValueError as e:
        if a.has("q"):
            return
        raise CmdError(str(e))
    data = text.encode("utf-8", "surrogateescape")
    if a.has("p"):
        ctx.write(data)
    else:
        _set_buffer(ctx, a.get("b"), data)


def cmd_clear_history(ctx: "Ctx", a: Args) -> None:
    _, pane = find_pane(ctx, a.get("t"))
    pane.term.screen.history.clear()


def cmd_send_keys(ctx: "Ctx", a: Args) -> None:
    s, pane = find_pane(ctx, a.get("t"))
    try:
        repeat = int(a.get("N") or 1)
    except ValueError:
        raise CmdError("repeat count invalid")
    if repeat < 1:
        raise CmdError("repeat count too small")
    host = ctx.host
    if a.has("X"):
        if pane.mode is None:
            raise CmdError("not in a mode")
        if not a.pos:
            raise CmdError("no command specified")
        host.mode_command(pane, a.pos[0], repeat)
        return
    if a.has("R"):
        pane.term.reset()
    if a.has("M") or a.has("K"):
        return  # a mouse event / client key from a binding: not ours to replay
    items = [_fmt(ctx, k, s) if a.has("F") else k for k in a.pos]
    data = bytearray()
    for _ in range(repeat):
        for k in items:
            if a.has("H"):
                try:
                    b = int(k, 16)
                    if not 0 <= b <= 0xFF:
                        raise ValueError
                except ValueError:
                    raise CmdError("invalid key: %s" % k)
                data.append(b)
            elif a.has("l"):
                data += k.encode("utf-8", "surrogateescape")
            else:
                kb = keys.key_bytes(k, pane.term.app_cursor, pane.term.keypad)
                data += kb if kb is not None else k.encode("utf-8", "surrogateescape")
    if not data:
        return
    if pane.mode is not None:
        host.mode_key(pane, bytes(data))
        return
    pane.write(bytes(data))


def cmd_copy_mode(ctx: "Ctx", a: Args) -> None:
    _, pane = find_pane(ctx, a.get("t"))
    host = ctx.host
    if a.has("q"):
        host.mode_exit(pane)
        return
    if a.has("M") or a.has("S"):
        return  # mouse-driven entry: a client the host does not draw
    host.mode_enter(pane, a.has("e"))
    if a.has("e"):
        pane.mode.exit_on_bottom = True
    if a.has("u"):
        host.mode_command(pane, "page-up")
    elif a.has("d"):
        host.mode_command(pane, "page-down")


# -- options / environment / hooks -----------------------------------------------------


def _option_scope(ctx: "Ctx", a: Args) -> Tuple[Dict[str, str], Optional["Session"]]:
    if a.has("g") or a.has("s"):
        return ctx.ns.options, None
    s = find_session(ctx, a.get("t"))
    return s.options, s


_BOOL_VALUES = {
    "on": "on",
    "off": "off",
    "yes": "on",
    "no": "off",
    "1": "on",
    "0": "off",
    "true": "on",
    "false": "off",
}


def cmd_set_option(ctx: "Ctx", a: Args) -> None:
    from backend.session.winmux.host import BOOL_OPTIONS

    if not a.pos:
        raise CmdError("command set-option: too few arguments (need at least 1)")
    store, session = _option_scope(ctx, a)
    name = a.pos[0]
    value = " ".join(a.pos[1:]) if len(a.pos) > 1 else None
    if a.has("u") or a.has("U"):
        store.pop(name, None)
    else:
        if a.has("o") and name in store:
            if a.has("q"):
                return
            raise CmdError("already set: %s" % name)
        if value is not None and a.has("F"):
            value = _fmt(ctx, value, session)
        if name in BOOL_OPTIONS:
            if value is None:
                cur = ctx.host.option(ctx.ns, session, name)
                value = "off" if cur == "on" else "on"
            else:
                v = _BOOL_VALUES.get(value.lower())
                if v is None:
                    raise CmdError("bad value: %s" % value)
                value = v
        elif name == "history-limit":
            if value is None or not value.isdigit():
                raise CmdError("value is invalid: %s" % value)
        elif value is None:
            value = ""
        if a.has("a") and name in store:
            value = store[name] + value
        store[name] = value
    targets = [session] if session is not None else list(ctx.ns.sessions.values())
    for s in targets:
        ctx.host.apply_options(s)


def cmd_set_window_option(ctx: "Ctx", a: Args) -> None:
    cmd_set_option(ctx, a)


def cmd_show_options(ctx: "Ctx", a: Args) -> None:
    from backend.session.winmux.host import OPTION_DEFAULTS

    if a.has("g") or a.has("s"):
        session, store = None, ctx.ns.options
    else:
        session = find_session(ctx, a.get("t"))
        store = session.options
    names = a.pos[:1]
    if names:
        name = names[0]
        if name in store:
            v = store[name]
        elif session is None and name in OPTION_DEFAULTS:
            v = OPTION_DEFAULTS[name]
        elif session is not None and a.has("A"):
            v = ctx.host.option(ctx.ns, session, name)
        elif name in OPTION_DEFAULTS or name.startswith("@"):
            return
        else:
            if a.has("q"):
                return
            raise CmdError("invalid option: %s" % name)
        ctx.print(v if a.has("v") else "%s %s" % (name, _quote_opt(v)))
        return
    items = dict(OPTION_DEFAULTS) if session is None else {}
    items.update(store)
    for k in sorted(items):
        ctx.print(items[k] if a.has("v") else "%s %s" % (k, _quote_opt(items[k])))


def cmd_show_window_options(ctx: "Ctx", a: Args) -> None:
    cmd_show_options(ctx, a)


def _quote_opt(v: str) -> str:
    return '"%s"' % v if (" " in v or v == "") else v


def cmd_set_environment(ctx: "Ctx", a: Args) -> None:
    if not a.pos:
        raise CmdError("command set-environment: too few arguments (need at least 1)")
    if a.has("g"):
        env = ctx.ns.env
    else:
        env = find_session(ctx, a.get("t")).env
    name = a.pos[0]
    if not name or "=" in name:
        raise CmdError("invalid environment variable: %s" % name)
    if a.has("u"):
        env.pop(name, None)
    elif a.has("r"):
        env[name] = None
    else:
        if len(a.pos) < 2:
            raise CmdError("no value specified")
        value = a.pos[1]
        if a.has("F"):
            value = _fmt(ctx, value)
        env[name] = value


def cmd_show_environment(ctx: "Ctx", a: Args) -> None:
    env = ctx.ns.env if a.has("g") else find_session(ctx, a.get("t")).env

    def show(k, v):
        if a.has("s"):
            if v is None:
                return "unset %s;" % k
            return '%s="%s"; export %s;' % (k, v.replace('"', '\\"'), k)
        return "-%s" % k if v is None else "%s=%s" % (k, v)

    if a.pos:
        k = a.pos[0]
        if k not in env:
            raise CmdError("unknown variable: %s" % k)
        ctx.print(show(k, env[k]))
        return
    for k in sorted(env):
        ctx.print(show(k, env[k]))


def cmd_set_hook(ctx: "Ctx", a: Args) -> None:
    if not a.pos:
        raise CmdError("command set-hook: too few arguments (need at least 1)")
    hooks = ctx.ns.hooks if a.has("g") else find_session(ctx, a.get("t")).hooks
    name = a.pos[0]
    m = re.match(r"^([a-z-]+)(?:\[(\d+)\])?$", name)
    if not m:
        raise CmdError("invalid hook: %s" % name)
    base = m.group(1)
    if a.has("u"):
        hooks.pop(base, None)
        return
    if a.has("R"):
        ctx.host.fire(
            ctx.ns, base, None if a.has("g") else find_session(ctx, a.get("t"))
        )
        return
    if len(a.pos) < 2:
        raise CmdError("no command specified")
    if a.has("a"):
        hooks.setdefault(base, []).append(a.pos[1])
    else:
        hooks[base] = [a.pos[1]]


def cmd_show_hooks(ctx: "Ctx", a: Args) -> None:
    hooks = ctx.ns.hooks if a.has("g") else find_session(ctx, a.get("t")).hooks
    for k in sorted(hooks):
        for i, cmd in enumerate(hooks[k]):
            ctx.print("%s[%d] %s" % (k, i, cmd))


def cmd_if_shell(ctx: "Ctx", a: Args) -> None:
    if len(a.pos) < 2:
        raise CmdError("command if-shell: too few arguments (need at least 2)")
    s = None
    try:
        s = find_session(ctx, a.get("t"))
    except CmdError:
        if a.has("t"):
            raise
    cond = a.pos[0]
    if a.has("F"):
        ok = formats.truthy(_fmt(ctx, cond, s))
    else:
        try:
            ok = (
                subprocess.run(
                    [compat.posix_shell(), "-c", _fmt(ctx, cond, s)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    creationflags=0x08000000 if compat.IS_WINDOWS else 0,
                ).returncode
                == 0
            )
        except (OSError, subprocess.SubprocessError):
            ok = False
    text = a.pos[1] if ok else (a.pos[2] if len(a.pos) > 2 else "")
    for cmd in parse_command_string(text):
        execute(ctx, cmd)


def cmd_bind_key(ctx: "Ctx", a: Args) -> None:
    if not a.pos:
        raise CmdError("command bind-key: too few arguments (need at least 1)")
    table = a.get("T") or ("root" if a.has("n") else "prefix")
    ctx.ns.bindings[(table, a.pos[0])] = list(a.pos[1:])


def cmd_unbind_key(ctx: "Ctx", a: Args) -> None:
    table = a.get("T") or ("root" if a.has("n") else "prefix")
    if a.has("a"):
        for k in [k for k in ctx.ns.bindings if k[0] == table]:
            del ctx.ns.bindings[k]
        return
    if a.pos:
        ctx.ns.bindings.pop((table, a.pos[0]), None)


def cmd_list_keys(ctx: "Ctx", a: Args) -> None:
    for (table, key), cmd in sorted(ctx.ns.bindings.items()):
        if a.get("T") and a.get("T") != table:
            continue
        ctx.print(
            "bind-key -T %s %s %s" % (table, key, " ".join(shlex.quote(c) for c in cmd))
        )


# -- buffers ---------------------------------------------------------------------------


def _buffer(ctx: "Ctx", name: Optional[str]):
    ns = ctx.ns
    if name:
        b = ns.buffers.get(name)
        if b is None:
            raise CmdError("unknown buffer: %s" % name)
        return b
    if not ns.buffers:
        return None
    return next(reversed(ns.buffers.values()))


def _path(ctx: "Ctx", path: str) -> str:
    path = proto.native_path(os.path.expanduser(path))
    if ctx.cwd and not os.path.isabs(path):
        path = os.path.join(ctx.cwd, path)
    return path


def cmd_load_buffer(ctx: "Ctx", a: Args) -> None:
    if not a.pos:
        raise CmdError("command load-buffer: too few arguments (need at least 1)")
    path = a.pos[0]
    if path == "-":
        data = ctx.stdin
    else:
        try:
            with open(_path(ctx, path), "rb") as f:
                data = f.read()
        except OSError as e:
            raise CmdError("%s: %s" % (path, e.strerror or e))
    _set_buffer(ctx, a.get("b"), data)


def cmd_set_buffer(ctx: "Ctx", a: Args) -> None:
    ns = ctx.ns
    name = a.get("b")
    if a.has("n"):
        b = _buffer(ctx, name)
        if b is None:
            raise CmdError("no buffer")
        new = a.get("n")
        del ns.buffers[b.name]
        b.name, b.auto = new, False
        ns.buffers[new] = b
        return
    if not a.pos:
        raise CmdError("command set-buffer: too few arguments (need at least 1)")
    data = a.pos[0].encode("utf-8", "surrogateescape")
    if a.has("a") and name and name in ns.buffers:
        data = ns.buffers[name].data + data
    _set_buffer(ctx, name, data)


def cmd_show_buffer(ctx: "Ctx", a: Args) -> None:
    b = _buffer(ctx, a.get("b"))
    if b is None:
        raise CmdError("no buffers")
    ctx.write(b.data)


def cmd_save_buffer(ctx: "Ctx", a: Args) -> None:
    b = _buffer(ctx, a.get("b"))
    if b is None:
        raise CmdError("no buffers")
    if not a.pos:
        raise CmdError("command save-buffer: too few arguments (need at least 1)")
    if a.pos[0] == "-":
        ctx.write(b.data)
        return
    try:
        with open(_path(ctx, a.pos[0]), "ab" if a.has("a") else "wb") as f:
            f.write(b.data)
    except OSError as e:
        raise CmdError("%s: %s" % (a.pos[0], e.strerror or e))


def cmd_list_buffers(ctx: "Ctx", a: Args) -> None:
    fmt = a.get("F") or '#{buffer_name}: #{buffer_size} bytes: "#{buffer_sample}"'
    for b in reversed(list(ctx.ns.buffers.values())):
        sample = b.data[:50].decode("utf-8", "replace").replace("\n", "\\n")
        extra = {
            "buffer_name": b.name,
            "buffer_size": str(len(b.data)),
            "buffer_sample": sample,
            "buffer_created": str(int(b.created)),
        }
        if _filter(ctx, a, extra=extra):
            ctx.print(_fmt(ctx, fmt, extra=extra))


def cmd_delete_buffer(ctx: "Ctx", a: Args) -> None:
    b = _buffer(ctx, a.get("b"))
    if b is None:
        raise CmdError("no buffer")
    del ctx.ns.buffers[b.name]


def cmd_paste_buffer(ctx: "Ctx", a: Args) -> None:
    s, pane = find_pane(ctx, a.get("t"))
    name = a.get("b")
    if name and name not in ctx.ns.buffers:
        raise CmdError("no buffer %s" % name)
    b = _buffer(ctx, name)
    if b is None:
        return
    data = b.data
    if not a.has("r"):
        sep = (a.get("s") if a.has("s") else "\r").encode("utf-8")
        data = data.replace(b"\n", sep)
    if a.has("p") and pane.term.bracketed_paste:
        data = b"\x1b[200~" + data + b"\x1b[201~"
    if a.has("d"):
        del ctx.ns.buffers[b.name]
    # Into the pane in any mode, as tmux does: copy mode only changes what
    # the clients see, and a message pasted while the person had scrolled up
    # must not be dropped.
    pane.write(data)


# -- size / clients ----------------------------------------------------------------------


def _size_args(a: Args, s: "Session") -> Tuple[int, int]:
    pane = s.pane
    rows, cols = (pane.term.rows, pane.term.cols) if pane else (24, 80)
    try:
        if a.get("x"):
            cols = int(a.get("x").rstrip("%"))
        if a.get("y"):
            rows = int(a.get("y").rstrip("%"))
    except ValueError:
        raise CmdError("width or height is invalid")
    return rows, cols


def cmd_resize_window(ctx: "Ctx", a: Args) -> None:
    s = find_session(ctx, a.get("t"))
    if a.has("A") or a.has("a"):
        s.manual_size = None
        s.options["window-size"] = "smallest" if a.has("a") else "largest"
        ctx.host.fit_size(s)
        return
    rows, cols = _size_args(a, s)
    # tmux: an explicit size makes the window's size manual until
    # window-size is set back (pane_scroll_find does exactly that).
    s.options["window-size"] = "manual"
    s.manual_size = (rows, cols)
    ctx.host.resize_pane(s, rows, cols)


def cmd_resize_pane(ctx: "Ctx", a: Args) -> None:
    s, _ = find_pane(ctx, a.get("t"))
    if a.has("x") or a.has("y"):
        rows, cols = _size_args(a, s)
        s.options["window-size"] = "manual"
        s.manual_size = (rows, cols)
        ctx.host.resize_pane(s, rows, cols)


def cmd_attach_session(ctx: "Ctx", a: Args) -> None:
    find_session(ctx, a.get("t"))
    raise CmdError("open terminal failed: not a terminal")


def cmd_detach_client(ctx: "Ctx", a: Args) -> None:
    host = ctx.host
    if a.has("s"):
        s = find_session(ctx, a.get("s"))
        for c in list(s.clients):
            host.detach_client(c, "[detached (from session %s)]\r\n" % s.name)
            host.fire(ctx.ns, "client-detached", s)
        return
    name = a.get("t")
    clients = [c for s in ctx.ns.sessions.values() for c in s.clients]
    if name:
        target = next((c for c in clients if c.name == name), None)
        if target is None:
            raise CmdError("can't find client: %s" % name)
    elif ctx.client is not None:
        target = ctx.client
    else:
        raise CmdError("no current client")
    victims = [c for c in clients if c is not target] if a.has("a") else [target]
    for c in victims:
        s = c.session
        host.detach_client(c, "[detached (from session %s)]\r\n" % s.name)
        host.fire(ctx.ns, "client-detached", s)


_TABLE: Dict[str, Callable] = {
    name: globals()["cmd_" + name.replace("-", "_")]
    for name in proto.COMMANDS
    if ("cmd_" + name.replace("-", "_")) in globals()
}
