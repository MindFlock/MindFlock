"""What the session host's client and daemon share: where the host's
rendezvous file lives, how a tmux argv splits into a command chain and its
options, and the wire format.

Wire format: one TCP connection per request to ``127.0.0.1:<port>``. The
client writes one JSON line (``{"op": …, "token": …, …}``); the host
answers one JSON line. A ``run`` answer carries ``rc``, and ``out`` /
``err`` base64-encoded (capture output is bytes, and may be large). After
an ``attach`` answer the connection becomes a stream: host → client raw
terminal bytes; client → host frames of ``<1 byte kind><4 byte length>
<payload>`` — ``d`` input, ``r`` resize (two big-endian uint16: rows,
cols), ``x`` detach.

Namespaces: tmux's ``-L name`` / ``-S path`` select an independent server.
The host is one daemon per user that keeps an independent *namespace* per
name instead — own sessions, options, environment, buffers and hooks, and
``kill-server`` ends only its own — so a stray ``-L`` cannot cost a second
daemon. ``-S`` paths whose basename is ``default`` or ``winmux`` (what
``$TMUX``'s first field reads inside a session) are the default namespace.
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import struct
from typing import Dict, List, Optional, Tuple

__all__ = [
    "PROTO",
    "VERSION_LINE",
    "Args",
    "CmdError",
    "COMMANDS",
    "canonical",
    "home_dir",
    "run_dir",
    "namespace_of",
    "parse_top",
    "socket_label",
    "split_chain",
    "native_path",
]

PROTO = 1
VERSION = "3.4"
VERSION_LINE = "tmux %s (winmux)" % VERSION


class CmdError(Exception):
    """A tmux command failed: the message tmux prints on stderr."""


def home_dir() -> str:
    """MindFlock's home: ``$MINDFLOCK_HOME``, else ``~/.mindflock`` — the
    same rule ``backend.providers.claude_background`` and
    ``backend.web.core.self_update`` use (not importable here: the host
    imports nothing POSIX-only and nothing heavy)."""
    home = os.environ.get("MINDFLOCK_HOME", "").strip()
    if home:
        return home
    base = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(base, ".mindflock")


def run_dir() -> str:
    return os.path.join(home_dir(), "run")


def native_path(path: str) -> str:
    """A path a Git Bash script may have written (``/c/Users/x``) as Windows
    reads it (``C:/Users/x``). Unchanged elsewhere."""
    if os.name == "nt":
        m = re.match(r"^/([A-Za-z])(/.*)?$", path)
        if m:
            return m.group(1).upper() + ":" + (m.group(2) or "/")
    return path


# -- argv ---------------------------------------------------------------------

#: tmux's top-level flags that take a value.
_TOP_WITH_ARG = set("cfLST")
_TOP_FLAGS = set("2CDlNuvV")


def parse_top(argv: List[str]) -> Tuple[Dict[str, str], List[str]]:
    """``argv`` (without ``"tmux"``) → ``({flag: value}, command argv)``."""
    flags: Dict[str, str] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-":
            break
        j = 1
        while j < len(a):
            c = a[j]
            if c in _TOP_WITH_ARG:
                val = a[j + 1 :]
                if not val:
                    i += 1
                    val = argv[i] if i < len(argv) else ""
                flags[c] = val
                break
            if c in _TOP_FLAGS:
                flags[c] = "1"
                j += 1
                continue
            raise CmdError("unknown option -- %s" % c)
        i += 1
    return flags, argv[i:]


def namespace_of(flags: Dict[str, str]) -> str:
    if "S" in flags:
        path = flags["S"]
        base = re.split(r"[\\/]", path.rstrip("/\\"))[-1] if path else ""
        if base in ("", "default", "winmux"):
            return "default"
        return path
    if "L" in flags:
        return flags["L"] or "default"
    return "default"


def socket_label(ns: str) -> str:
    """What "no server running on …" names for a namespace."""
    if ns == "default":
        return os.path.join(run_dir(), "winmux-default")
    if re.search(r"[\\/]", ns):
        return ns
    return os.path.join(run_dir(), "winmux-" + ns)


def split_chain(argv: List[str]) -> List[List[str]]:
    """tmux's command sequences: an argument ``;`` ends a command, as does a
    trailing ``;`` on an argument (which is dropped); ``\\;`` at the end is a
    literal ``;`` (cmd-parse.c's ``cmd_parse_from_arguments``)."""
    cmds: List[List[str]] = []
    cur: List[str] = []
    for a in argv:
        if a.endswith(";"):
            body = a[:-1]
            if body.endswith("\\"):
                cur.append(body[:-1] + ";")
                continue
            if body:
                cur.append(body)
            if cur:
                cmds.append(cur)
            cur = []
            continue
        cur.append(a)
    if cur:
        cmds.append(cur)
    return cmds


#: Every command the host knows: canonical name → (aliases, getopt string).
#: Option strings are tmux 3.4's.
COMMANDS: Dict[str, Tuple[Tuple[str, ...], str]] = {
    "attach-session": (("attach", "a"), "c:dEf:rt:x"),
    "bind-key": (("bind",), "nrN:T:"),
    "capture-pane": (("capturep",), "ab:CeE:JNpPqS:t:"),
    "clear-history": (("clearhist",), "Ht:"),
    "copy-mode": ((), "deHMqSs:t:u"),
    "delete-buffer": (("deleteb",), "b:"),
    "detach-client": (("detach",), "aE:s:t:P"),
    "display-message": (("display",), "aCc:d:lINpt:F:v"),
    "has-session": (("has",), "t:"),
    "if-shell": (("if",), "bFt:"),
    "kill-pane": (("killp",), "at:"),
    "kill-server": ((), ""),
    "kill-session": ((), "aCt:"),
    "kill-window": (("killw",), "at:"),
    "list-buffers": (("lsb",), "F:f:"),
    "list-clients": (("lsc",), "F:f:t:"),
    "list-keys": (("lsk",), "1aNP:T:"),
    "list-panes": (("lsp",), "aF:f:st:"),
    "list-sessions": (("ls",), "F:f:"),
    "list-windows": (("lsw",), "aF:f:t:"),
    "load-buffer": (("loadb",), "b:t:w"),
    "new-session": (("new",), "Ac:dDe:EF:f:n:Ps:t:x:Xy:"),
    "paste-buffer": (("pasteb",), "db:prs:t:"),
    "refresh-client": (("refresh",), "A:B:cC:Df:F:l:LRSt:U"),
    "rename-session": (("rename",), "t:"),
    "rename-window": (("renamew",), "t:"),
    "resize-pane": (("resizep",), "DLMRTt:Ux:y:Z"),
    "resize-window": (("resizew",), "aADLRt:Ux:y:"),
    "save-buffer": (("saveb",), "ab:"),
    "select-pane": (("selectp",), "DdegLlMmP:RT:t:UZ"),
    "select-window": (("selectw",), "lnpTt:"),
    "send-keys": (("send",), "c:FHKlMN:Rt:X"),
    "set-buffer": (("setb",), "aAb:Lln:t:w"),
    "set-environment": (("setenv",), "Fhgrt:u"),
    "set-hook": ((), "agpRt:uw"),
    "set-option": (("set",), "aFgopqst:uUw"),
    "set-window-option": (("setw",), "aFgoqt:u"),
    "show-buffer": (("showb",), "b:"),
    "show-environment": (("showenv",), "hgst:"),
    "show-hooks": ((), "gpt:w"),
    "show-options": (("show",), "AgHpqst:vw"),
    "show-window-options": (("showw",), "gvt:"),
    "start-server": (("start",), ""),
    "unbind-key": (("unbind",), "anqT:"),
}

_ALIAS: Dict[str, str] = {}
for _name, (_aliases, _spec) in COMMANDS.items():
    _ALIAS[_name] = _name
    for _a in _aliases:
        if _a:
            _ALIAS[_a] = _name


def canonical(name: str) -> Optional[str]:
    """A command name or alias → its canonical name; a unique prefix works
    too, as in tmux. ``None`` for an unknown command."""
    if name in _ALIAS:
        return _ALIAS[name]
    hits = sorted({c for a, c in _ALIAS.items() if a.startswith(name)})
    if len(hits) == 1:
        return hits[0]
    return None


class Args:
    """A command's argv parsed POSIX-getopt style against its tmux option
    string: grouped flags (``-dP``), attached values (``-tfoo``), ``--``,
    and options stop at the first positional argument."""

    def __init__(self, argv: List[str], spec: str) -> None:
        takes = {}
        k = 0
        while k < len(spec):
            c = spec[k]
            arg = k + 1 < len(spec) and spec[k + 1] == ":"
            takes[c] = arg
            k += 2 if arg else 1
        self.flags: Dict[str, List[str]] = {}
        i = 0
        while i < len(argv):
            a = argv[i]
            if a == "--":
                i += 1
                break
            if not a.startswith("-") or a == "-":
                break
            j = 1
            while j < len(a):
                c = a[j]
                if c not in takes:
                    raise CmdError("unknown flag -%s" % c)
                if takes[c]:
                    val = a[j + 1 :]
                    if not val:
                        i += 1
                        if i >= len(argv):
                            raise CmdError("-%s expects an argument" % c)
                        val = argv[i]
                    self.flags.setdefault(c, []).append(val)
                    break
                self.flags.setdefault(c, []).append("")
                j += 1
            i += 1
        self.pos: List[str] = argv[i:]

    def has(self, c: str) -> bool:
        return c in self.flags

    def get(self, c: str, default: Optional[str] = None) -> Optional[str]:
        v = self.flags.get(c)
        return v[-1] if v else default

    def all(self, c: str) -> List[str]:
        return list(self.flags.get(c, []))


# -- wire ---------------------------------------------------------------------


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(s: Optional[str]) -> bytes:
    return base64.b64decode(s) if s else b""


def send_json(sock: socket.socket, obj: dict) -> None:
    sock.sendall(json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n")


def recv_line(sock: socket.socket, buf: bytearray, limit: int = 1 << 30) -> bytes:
    """One ``\\n``-terminated line from ``sock`` (``buf`` keeps what was read
    past it). ``ConnectionError`` if the peer closes first."""
    while True:
        k = buf.find(b"\n")
        if k >= 0:
            line = bytes(buf[:k])
            del buf[: k + 1]
            return line
        if len(buf) > limit:
            raise ConnectionError("line too long")
        chunk = sock.recv(1 << 16)
        if not chunk:
            raise ConnectionError("connection closed")
        buf.extend(chunk)


def frame(kind: bytes, payload: bytes = b"") -> bytes:
    return kind + struct.pack(">I", len(payload)) + payload


def resize_frame(rows: int, cols: int) -> bytes:
    return frame(b"r", struct.pack(">HH", max(1, rows) & 0xFFFF, max(1, cols) & 0xFFFF))
