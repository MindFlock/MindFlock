"""In-sandbox TCP -> unix-socket forwarder (stdlib only, no backend imports).

Copied into ``run/bridge.py`` and started inside the sandbox as::

    python3 -I bridge.py <run>/egress.sock <port>

It binds ``127.0.0.1:<port>`` in the sandbox's own network namespace (so
``HTTPS_PROXY=http://127.0.0.1:<port>`` works for the agent), then forks: the
parent exits 0 once the port is listening, so the launcher can ``exec`` the
agent without racing the bridge; the child splices every connection to the
host's egress proxy socket. All policy lives in the host proxy; this file
only moves bytes.
"""

import asyncio
import os
import socket
import sys

MAX_CONNS = 128


async def _pipe(src, dst):
    try:
        while True:
            data = await src.read(65536)
            if not data:
                break
            dst.write(data)
            await dst.drain()
        if dst.can_write_eof():
            dst.write_eof()
    except (ConnectionError, OSError, RuntimeError):
        pass


_DIR_FDS = {}


def _short(path):
    """A connect() address for ``path`` even past sun_path's 108 bytes: a
    long path goes through /proc/self/fd/<dirfd>/<name>; the O_PATH dir fd
    is opened once and kept for the bridge's lifetime."""
    if len(os.fsencode(path)) <= 100:
        return path
    d = os.path.dirname(path) or "."
    fd = _DIR_FDS.get(d)
    if fd is None:
        fd = _DIR_FDS[d] = os.open(d, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    return "/proc/self/fd/%d/%s" % (fd, os.path.basename(path))


async def _serve(sock, upstream):
    active = [0]

    async def handle(reader, writer):
        if active[0] >= MAX_CONNS:
            writer.close()
            return
        active[0] += 1
        up_w = None
        try:
            up_r, up_w = await asyncio.open_unix_connection(_short(upstream))
            await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
        except (ConnectionError, OSError):
            pass
        finally:
            active[0] -= 1
            if up_w is not None:
                up_w.close()
            writer.close()

    server = await asyncio.start_server(handle, sock=sock)
    async with server:
        await server.serve_forever()


def main(argv):
    if len(argv) != 3 or not argv[2].isdigit():
        sys.stderr.write("usage: bridge.py <egress.sock> <port>\n")
        return 64
    upstream, port = argv[1], int(argv[2])
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(64)
    except OSError as exc:
        sys.stderr.write(f"bridge: cannot listen on 127.0.0.1:{port}: {exc}\n")
        return 71
    sock.setblocking(False)
    if os.fork() > 0:
        return 0  # listening; let the launcher continue
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)  # never scribble over the agent's TUI
    try:
        asyncio.run(_serve(sock, upstream))
    except BaseException:
        pass
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
