"""Shared async subprocess runner for the ingestion pipeline.

One copy of the capture / kill / reap / timeout convention used by the
provisioner, PR provisioner and cache refresher, so the three cannot drift.
On timeout the process is killed and reaped and ``rc 124`` is returned with a
stderr message — each caller's existing ``rc != 0`` handling turns that into
its own normal error.
"""

from __future__ import annotations

import asyncio


async def run_capture(
    *args: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float,
) -> tuple[int, bytes, bytes]:
    """Run ``args`` to completion, capturing its output.

    Returns ``(returncode, stdout, stderr)``. ``cwd``/``env`` are forwarded to
    the child only when given (otherwise it inherits the parent's). On timeout
    the child is killed and reaped and ``(124, b"", <message>)`` is returned, so
    each caller's existing ``rc != 0`` handling turns the timeout into that
    caller's own normal error.
    """
    kwargs: dict = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if cwd is not None:
        kwargs["cwd"] = cwd
    if env is not None:
        kwargs["env"] = env
    proc = await asyncio.create_subprocess_exec(*args, **kwargs)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.communicate()  # reap the killed process
        except ProcessLookupError:
            pass
        # rc 124 + stderr message: callers' existing rc != 0 handling turns
        # this into their normal error.
        return 124, b"", f"{' '.join(args)} timed out after {timeout:.0f}s".encode()
    return proc.returncode or 0, stdout, stderr
