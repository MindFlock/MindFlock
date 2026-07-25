"""Hermetic tests for the shared async subprocess runner.

``run_capture`` is the single copy of the capture / kill / reap / timeout
convention shared by the provisioner, PR provisioner and cache refresher. The
timeout path (kill + reap + ``rc 124``) is the interesting, previously-uncovered
behaviour; the success path just forwards the child's rc/stdout/stderr.

No real subprocesses are spawned — ``asyncio.create_subprocess_exec`` is patched
to hand back a fake process object.
"""

from unittest.mock import AsyncMock, patch

import pytest

from backend.ticket_ingestion._subprocess import run_capture


class _FakeProc:
    """A stand-in asyncio subprocess.

    ``hang`` makes the FIRST ``communicate()`` block long enough for
    ``asyncio.wait_for`` to time out and cancel it; the reap (second call)
    returns immediately, or raises ``ProcessLookupError`` when
    ``reap_raises`` is set (the process died between kill and reap).
    """

    def __init__(
        self,
        returncode=0,
        stdout=b"",
        stderr=b"",
        hang=False,
        reap_raises=False,
    ):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self._reap_raises = reap_raises
        self.killed = False
        self._calls = 0

    def kill(self):
        self.killed = True

    async def communicate(self):
        import asyncio

        self._calls += 1
        if self._hang and self._calls == 1:
            await asyncio.sleep(3600)  # cancelled by wait_for's timeout
        if self._reap_raises and self._calls >= 2:
            raise ProcessLookupError()
        return self._stdout, self._stderr


def _patch_spawn(proc):
    return patch(
        "asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=proc,
    )


class TestRunCaptureSuccess:
    async def test_forwards_rc_and_streams(self):
        proc = _FakeProc(returncode=0, stdout=b"out", stderr=b"err")
        with _patch_spawn(proc):
            rc, out, err = await run_capture("git", "status", timeout=5)
        assert (rc, out, err) == (0, b"out", b"err")

    async def test_nonzero_returncode_preserved(self):
        proc = _FakeProc(returncode=3)
        with _patch_spawn(proc):
            rc, _, _ = await run_capture("git", "clone", timeout=5)
        assert rc == 3

    async def test_none_returncode_becomes_zero(self):
        # A child whose returncode is still None (never set) must read as 0,
        # not crash callers that do `rc != 0`.
        proc = _FakeProc(returncode=None)
        with _patch_spawn(proc):
            rc, _, _ = await run_capture("git", "status", timeout=5)
        assert rc == 0

    async def test_cwd_and_env_forwarded_when_given(self):
        proc = _FakeProc()
        with _patch_spawn(proc) as spawn:
            await run_capture("git", "log", cwd="/some/dir", env={"A": "1"}, timeout=5)
        kwargs = spawn.await_args.kwargs
        assert kwargs["cwd"] == "/some/dir"
        assert kwargs["env"] == {"A": "1"}
        assert kwargs["stdout"] is not None and kwargs["stderr"] is not None

    async def test_cwd_and_env_omitted_when_none(self):
        # When cwd/env are None they must NOT be passed through — the child
        # inherits the parent's cwd/environment.
        proc = _FakeProc()
        with _patch_spawn(proc) as spawn:
            await run_capture("git", "log", timeout=5)
        kwargs = spawn.await_args.kwargs
        assert "cwd" not in kwargs
        assert "env" not in kwargs


class TestRunCaptureTimeout:
    async def test_timeout_kills_reaps_and_returns_124(self):
        proc = _FakeProc(hang=True)
        with _patch_spawn(proc):
            rc, out, err = await run_capture("git", "clone", timeout=0.05)
        assert rc == 124
        assert out == b""
        # The stderr message names the command and the timeout.
        assert b"git clone timed out after" in err
        assert proc.killed is True
        # Reaped: communicate called twice (the timed-out wait + the reap).
        assert proc._calls == 2

    async def test_reap_process_lookup_error_swallowed(self):
        # The killed child can vanish before the reap; ProcessLookupError must
        # be swallowed and the 124 result still returned.
        proc = _FakeProc(hang=True, reap_raises=True)
        with _patch_spawn(proc):
            rc, out, err = await run_capture("git", "fetch", timeout=0.05)
        assert rc == 124
        assert proc.killed is True
        assert b"git fetch timed out after" in err


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
