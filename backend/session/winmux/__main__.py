"""``tmux`` for a person or a script on Windows — the in-session shim's
entry (the host puts a ``tmux`` / ``tmux.cmd`` running this on every
session's PATH) and ``python -m backend.session.winmux <tmux args>``.

Commands go to the host with this process's stdio (``load-buffer -`` reads
stdin); ``attach-session`` — and ``new-session`` without ``-d`` from a
console — become a foreground client in this console.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional

__all__ = ["main"]


def main(argv: Optional[List[str]] = None) -> int:
    from backend.session.winmux import client, proto

    args = list(sys.argv[1:] if argv is None else argv)
    try:
        flags, cmdv = proto.parse_top(args)
    except proto.CmdError as e:
        sys.stderr.write("%s\n" % e)
        return 1
    ns = proto.namespace_of(flags)
    chain = proto.split_chain(cmdv)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if "V" not in flags and len(chain) == 1:
        name = proto.canonical(chain[0][0])
        try:
            a = proto.Args(chain[0][1:], proto.COMMANDS[name][1]) if name else None
        except proto.CmdError:
            a = None
        if name == "attach-session" and a is not None:
            return client.console_attach(
                a.get("t"), readonly=a.has("r"), ns=ns, detach_others=a.has("d")
            )
        if name == "new-session" and a is not None and not a.has("d") and interactive:
            rc, out, err = client.execute(
                ["tmux"]
                + args[: len(args) - len(cmdv)]
                + [chain[0][0], "-d", "-P", "-F", "#{session_name}"]
                + chain[0][1:]
            )
            if rc != 0:
                sys.stderr.buffer.write(err)
                return rc
            return client.console_attach(out.decode().strip(), ns=ns)
    stdin = b""
    if any(
        c and proto.canonical(c[0]) == "load-buffer" and c[-1:] == ["-"] for c in chain
    ):
        stdin = sys.stdin.buffer.read()
    rc, out, err = client.execute(["tmux"] + args, stdin=stdin, env=dict(os.environ))
    if out:
        sys.stdout.buffer.write(out)
        sys.stdout.buffer.flush()
    if err:
        sys.stderr.buffer.write(err)
        sys.stderr.buffer.flush()
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
