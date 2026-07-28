"""P0: no subprocess or HTTP call may hang forever.

Covers the timeout plumbing added to the ``cmd`` executor, the git worktree
command runner, the provisioning ``_run`` helper, the web server's
``_run_capped`` wrapper, and the shared aiohttp client timeout constant.
"""

from __future__ import annotations

import subprocess

import pytest

from backend import cmd


class TestExecTimeout:
    def test_run_times_out_and_returns_error(self):
        err = cmd.Exec().run(cmd.command("sleep", "5"), timeout=0.2)
        assert isinstance(err, RuntimeError)
        assert "timed out after" in str(err)
        assert "sleep 5" in str(err)

    def test_output_times_out_and_returns_error(self):
        out, err = cmd.Exec().output(cmd.command("sleep", "5"), timeout=0.2)
        assert isinstance(out, bytes)
        assert isinstance(err, RuntimeError)
        assert "timed out after" in str(err)

    def test_run_success_unaffected(self):
        assert cmd.Exec().run(cmd.command("true")) is None

    def test_output_success_unaffected(self):
        out, err = cmd.Exec().output(cmd.command("echo", "hi"))
        assert err is None
        assert out.strip() == b"hi"


class TestRunGitCommandTimeout:
    def test_timeout_becomes_git_command_failed(self, monkeypatch):
        from backend.session.git import worktree_git

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

        monkeypatch.setattr(worktree_git.subprocess, "run", fake_run)

        class Host(worktree_git.GitWorktreeGitMixin):
            pass

        with pytest.raises(
            RuntimeError, match=r"git command failed: .*timed out after"
        ):
            Host().run_git_command("/tmp", "status")


class TestProvisionRunTimeout:
    def test_timeout_reports_failed_process(self):
        from backend.session import provisioned

        cp = provisioned._run("sleep", "5", timeout=0.2)
        assert cp.returncode == 124
        assert b"timed out after" in cp.stdout

    def test_timeout_with_check_raises_provision_error(self):
        from backend.session import provisioned

        with pytest.raises(provisioned.ProvisionError, match="timed out after"):
            provisioned._run("sleep", "5", timeout=0.2, check=True)


class TestServerRunCapped:
    def test_timeout_reports_rc_124(self):
        from backend.web import server

        cp = server._run_capped(
            ["sleep", "5"],
            timeout=0.2,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert cp.returncode == 124
        assert b"timed out after" in cp.stderr

    def test_timeout_text_mode_coerces_str(self):
        from backend.web import server

        cp = server._run_capped(
            ["sleep", "5"],
            timeout=0.2,
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 124
        assert "timed out after" in cp.stderr

    def test_success_passthrough(self):
        from backend.web import server

        cp = server._run_capped(["echo", "ok"], timeout=10, stdout=subprocess.PIPE)
        assert cp.returncode == 0
        assert cp.stdout.strip() == b"ok"


class TestAiohttpTimeouts:
    def test_ingestion_http_timeout_constants(self):
        from backend.ticket_ingestion import pr_comments, pr_monitor
        from backend.ticket_ingestion.providers import (
            asana,
            github_issues,
            jira,
            linear,
            shortcut,
        )

        for mod in (
            pr_monitor,
            pr_comments,
            shortcut,
            github_issues,
            asana,
            jira,
            linear,
        ):
            assert mod._HTTP_TIMEOUT.total == 30, mod.__name__
