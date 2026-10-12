"""Subprocess helpers shared by the server and the core modules.

``backend.web.server`` re-exports :func:`run_capped` as ``_run_capped`` for its
own call sites; core modules call it here (``proc.run_capped(...)``, read off
the module so a test's ``monkeypatch.setattr(proc, "run_capped", …)`` reaches
them).
"""

from __future__ import annotations

import subprocess


def run_capped(args, *, timeout, **kw):
    """``subprocess.run`` with a hard timeout that never raises on expiry.

    A hung child is killed and reported as a *failed* ``CompletedProcess``
    (returncode 124, the timeout note appended to stdout/stderr), so every
    call site's existing non-zero-exit handling applies unchanged.
    """
    try:
        return subprocess.run(args, timeout=timeout, **kw)
    except subprocess.TimeoutExpired as err:
        msg = "timed out after %gs: %s" % (timeout, " ".join(str(a) for a in args))
        text = bool(kw.get("text") or kw.get("universal_newlines"))

        def _coerce(v):
            if v is None:
                return msg if text else msg.encode("utf-8")
            if isinstance(v, bytes):
                return (
                    (v.decode("utf-8", "replace") + "\n" + msg)
                    if text
                    else v + b"\n" + msg.encode("utf-8")
                )
            return v + "\n" + msg

        return subprocess.CompletedProcess(
            args, 124, stdout=_coerce(err.output), stderr=_coerce(err.stderr)
        )
