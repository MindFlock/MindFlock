"""Console entry point (C3) + launcher guards (C4): parsing, delegation, doctor
output/exit codes, missing-web-deps hint, and the source-repo CWD guard."""

from __future__ import annotations

import io
import json
import os
import sys
import types
import urllib.error

import pytest

import backend.web.run as run
from backend import cli, client, doctor
from backend.doctor import Check


class TestServeParsing:
    @pytest.fixture()
    def serve_calls(self, monkeypatch):
        calls = []
        monkeypatch.setattr(run, "main", lambda argv=None: calls.append(argv))
        return calls

    def test_bare_invocation_serves_with_defaults(self, serve_calls):
        assert cli.main([]) == 0
        assert serve_calls == [[]]

    def test_serve_mode_and_port_forwarded(self, serve_calls):
        assert cli.main(["serve", "local", "--port", "9000"]) == 0
        assert serve_calls == [["local", "9000"]]

    def test_serve_tailscale(self, serve_calls):
        cli.main(["serve", "tailscale"])
        assert serve_calls == [["tailscale"]]

    def test_serve_rejects_unknown_mode(self, serve_calls):
        with pytest.raises(SystemExit):
            cli.main(["serve", "bogus"])
        assert serve_calls == []


class TestDoctorCommand:
    def test_exit_zero_when_only_warns(self, monkeypatch, capsys):
        monkeypatch.setattr(
            doctor,
            "run_checks",
            lambda: [
                Check("git", "git", "ok", "git version 2.43.0"),
                Check(
                    "uv", "uv", "warn", "not found", "curl https://astral.sh/uv | sh"
                ),
            ],
        )
        assert cli.main(["doctor"]) == 0
        out = capsys.readouterr().out
        assert "✓" in out and "!" in out
        assert "fix: curl https://astral.sh/uv | sh" in out

    def test_exit_one_and_fix_shown_on_failure(self, monkeypatch, capsys):
        monkeypatch.setattr(
            doctor,
            "run_checks",
            lambda: [
                Check(
                    "tmux", "tmux", "fail", "not found on PATH", "sudo apt install tmux"
                )
            ],
        )
        assert cli.main(["doctor"]) == 1
        out = capsys.readouterr().out
        assert "✗" in out
        assert "fix: sudo apt install tmux" in out

    def test_missing_gh_prints_info_not_a_failure(self, monkeypatch, capsys):
        # gh is optional: absent, it must print the `-` info glyph, keep exit 0
        # and never render as ✗. The installer runs this command and honours the
        # exit code, so a ✗ here would make gh a de-facto requirement for people
        # who only ever `git push` over their own SSH remote.
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        monkeypatch.setattr(doctor, "run_checks", lambda: [doctor.CHECKS_BY_ID["gh"]()])
        assert cli.main(["doctor"]) == 0
        out = capsys.readouterr().out
        assert "✗" not in out
        assert "- GitHub CLI (gh)" in out
        assert "pushing uses plain git" in out
        assert "All required dependencies look good." in out

    def test_help_does_not_list_gh_as_required(self):
        # The subcommand help is the first place anyone learns what MindFlock
        # needs; gh belongs in the optional parenthetical, not the required list.
        # Collapse whitespace: argparse wraps the subcommand help column.
        help_text = " ".join(cli._build_parser().format_help().split())
        assert "check git/tmux/agent-CLI (plus optional gh, uv, tailscale)" in help_text
        assert "optional gh" in help_text
        assert "git/tmux/gh" not in help_text


class TestDoctorFix:
    _FAIL = Check(
        "tmux",
        "tmux",
        "fail",
        "not found on PATH",
        "sudo apt install tmux",
        cmd="sudo apt install tmux",
    )

    @pytest.fixture()
    def tty_stdin(self, monkeypatch):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def test_accepted_fix_runs_command_and_rechecks(
        self, monkeypatch, capsys, tty_stdin
    ):
        monkeypatch.setattr(doctor, "run_checks", lambda: [self._FAIL])
        ran = []
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda cmd, shell: (
                ran.append((cmd, shell)),
                type("P", (), {"returncode": 0})(),
            )[1],
        )
        monkeypatch.setattr(
            doctor,
            "CHECKS_BY_ID",
            {"tmux": lambda: Check("tmux", "tmux", "ok", "tmux 3.4")},
        )
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        assert cli.main(["doctor", "--fix"]) == 0
        assert ran == [("sudo apt install tmux", True)]
        out = capsys.readouterr().out
        assert "tmux 3.4" in out
        assert "All required dependencies look good." in out

    def test_declined_fix_runs_nothing_and_keeps_exit_one(
        self, monkeypatch, capsys, tty_stdin
    ):
        monkeypatch.setattr(doctor, "run_checks", lambda: [self._FAIL])
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda *a, **k: pytest.fail("declined fix must not run"),
        )
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        assert cli.main(["doctor", "--fix"]) == 1
        assert "skipped" in capsys.readouterr().out

    def test_non_tty_prints_hint_instead_of_prompting(self, monkeypatch, capsys):
        monkeypatch.setattr(doctor, "run_checks", lambda: [self._FAIL])
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda *a, **k: pytest.fail("non-tty must not run fixes"),
        )
        assert cli.main(["doctor", "--fix"]) == 1
        assert "interactive terminal" in capsys.readouterr().out

    def test_checks_without_cmd_are_not_offered(self, monkeypatch, capsys, tty_stdin):
        monkeypatch.setattr(
            doctor,
            "run_checks",
            lambda: [Check("agent-cli", "agent CLI", "fail", "gone", "see Settings")],
        )
        monkeypatch.setattr(
            "builtins.input",
            lambda prompt="": pytest.fail("no runnable cmd — must not prompt"),
        )
        assert cli.main(["doctor", "--fix"]) == 1

    def test_eof_at_prompt_stops_the_loop(self, monkeypatch, capsys, tty_stdin):
        # A closed stdin (EOF) mid-loop aborts the remaining prompts cleanly.
        second = Check(
            "gh", "gh", "warn", "no auth", "gh auth login", cmd="gh auth login"
        )
        monkeypatch.setattr(doctor, "run_checks", lambda: [self._FAIL, second])

        def _eof(prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", _eof)
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda *a, **k: pytest.fail("EOF must stop before running anything"),
        )
        assert cli.main(["doctor", "--fix"]) == 1  # tmux still failing

    def test_command_nonzero_and_still_unhealthy_are_reported(
        self, monkeypatch, capsys, tty_stdin
    ):
        monkeypatch.setattr(doctor, "run_checks", lambda: [self._FAIL])
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda cmd, shell: type("P", (), {"returncode": 1})(),
        )
        # Re-probe still finds tmux broken (common when PATH needs a new shell).
        still = Check("tmux", "tmux", "fail", "still missing", docs="https://tmux.io")
        monkeypatch.setattr(doctor, "CHECKS_BY_ID", {"tmux": lambda: still})
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        assert cli.main(["doctor", "--fix"]) == 1
        out = capsys.readouterr().out
        assert "command exited 1" in out
        assert "still not healthy" in out
        assert "https://tmux.io" in out  # docs link appended when present

    def test_recheck_missing_id_still_runs_but_skips_reprobe(
        self, monkeypatch, capsys, tty_stdin
    ):
        check = Check("mystery", "mystery", "fail", "gone", "do-thing", cmd="do-thing")
        monkeypatch.setattr(doctor, "run_checks", lambda: [check])
        ran = []
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda cmd, shell: (
                ran.append(cmd),
                type("P", (), {"returncode": 0})(),
            )[1],
        )
        monkeypatch.setattr(doctor, "CHECKS_BY_ID", {})  # id absent → no re-probe
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        assert cli.main(["doctor", "--fix"]) == 1  # original fail unchanged
        assert ran == ["do-thing"]  # the fix still ran
        assert "still not healthy" not in capsys.readouterr().out

    def test_reprobe_exception_is_swallowed(self, monkeypatch, capsys, tty_stdin):
        monkeypatch.setattr(doctor, "run_checks", lambda: [self._FAIL])
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda cmd, shell: type("P", (), {"returncode": 0})(),
        )

        def _broken():
            raise RuntimeError("re-probe blew up")

        monkeypatch.setattr(doctor, "CHECKS_BY_ID", {"tmux": _broken})
        monkeypatch.setattr("builtins.input", lambda prompt="": "")  # Enter = yes
        # A broken re-probe must not kill the loop or the command.
        assert cli.main(["doctor", "--fix"]) == 1
        assert "still not healthy" not in capsys.readouterr().out


class TestWebDepsGuard:
    def test_missing_uvicorn_prints_hint_not_traceback(self, monkeypatch, capsys):
        class _Block:
            def find_spec(self, name, path=None, target=None):
                if name == "uvicorn":
                    raise ModuleNotFoundError(
                        f"No module named '{name}'", name="uvicorn"
                    )
                return None

        monkeypatch.delitem(sys.modules, "uvicorn", raising=False)
        monkeypatch.setattr(sys, "meta_path", [_Block()] + sys.meta_path)
        with pytest.raises(SystemExit) as exc:
            run.main([])
        assert exc.value.code == 1
        assert "uv sync --group web" in capsys.readouterr().err


class TestLocalModeEnvExport:
    """F7: run.main exports the resolved mode so the server's startup banner
    can suppress the tailnet URLs + QR that a 127.0.0.1 bind can't serve."""

    @pytest.fixture()
    def uvicorn_spy(self, monkeypatch):
        import uvicorn

        calls = []
        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
        # The double-launch guard probes the real port — a dev machine with a
        # live server on 8765 would short-circuit main() before uvicorn.run.
        monkeypatch.setattr(run, "_port_squatter", lambda host, port: "")
        return calls

    def test_default_mode_is_local_bound_loopback(self, uvicorn_spy, monkeypatch):
        """Security default: a bare launch binds 127.0.0.1 — exposure beyond
        this machine (tailscale mode, 0.0.0.0) must be an explicit opt-in."""
        import os

        monkeypatch.setenv("CS_WEB_MODE", "")
        monkeypatch.setenv("UVICORN_PORT", "")
        run.main([])
        assert os.environ.get("CS_WEB_MODE") == "local"
        assert uvicorn_spy[0]["host"] == "127.0.0.1"

    def test_local_mode_exported_and_bound_loopback(self, uvicorn_spy, monkeypatch):
        import os

        # setenv("") registers a restore, so run.main's exports can't leak into
        # other tests ("" is falsy, so the defaults still apply).
        monkeypatch.setenv("CS_WEB_MODE", "")
        monkeypatch.setenv("UVICORN_PORT", "")
        run.main(["local", "9123"])
        assert os.environ.get("CS_WEB_MODE") == "local"
        assert uvicorn_spy[0]["host"] == "127.0.0.1"
        assert uvicorn_spy[0]["port"] == 9123

    def test_cli_arg_overrides_env_mode(self, uvicorn_spy, monkeypatch):
        import os

        monkeypatch.setenv("CS_WEB_MODE", "local")
        monkeypatch.setenv("UVICORN_PORT", "")
        run.main(["tailscale"])
        assert os.environ.get("CS_WEB_MODE") == "tailscale"
        assert uvicorn_spy[0]["host"] == "0.0.0.0"


class TestSourceRepoGuard:
    def test_detected_by_package_layout(self, tmp_path):
        (tmp_path / "src" / "mindflock").mkdir(parents=True)
        assert run._is_mindflock_source_repo(tmp_path) is True

    def test_detected_by_pyproject_name(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "mindflock"\n')
        assert run._is_mindflock_source_repo(tmp_path) is True

    def test_other_project_not_flagged(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "someapp"\n')
        assert run._is_mindflock_source_repo(tmp_path) is False

    def test_empty_dir_not_flagged(self, tmp_path):
        assert run._is_mindflock_source_repo(tmp_path) is False


# --------------------------------------------------------------------------- #
# J1 — terminal-first session commands (new/ls/attach/open/events)
# --------------------------------------------------------------------------- #
def _fake_response(payload, code=200):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()

    return _Resp()


class TestClientProbe:
    def test_mindflock_fingerprint_accepted(self, monkeypatch):
        # Fingerprint = default_program + caps (repo_root was dropped from
        # /api/config in the 2026-07 legacy cleanup).
        cfg = {"default_program": "claude", "caps": {"git": True}, "home": "/h"}
        monkeypatch.setattr(
            "urllib.request.urlopen", lambda req, timeout=None: _fake_response(cfg)
        )
        assert client.probe("http://127.0.0.1:8765") == cfg

    def test_non_mindflock_service_rejected(self, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout=None: _fake_response({"hello": "world"}),
        )
        assert client.probe("http://127.0.0.1:8765") is None

    def test_dead_port_is_none(self, monkeypatch):
        def _boom(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr("urllib.request.urlopen", _boom)
        assert client.probe("http://127.0.0.1:8765") is None

    def test_http_error_body_surfaced_as_api_error(self, monkeypatch):
        err = urllib.error.HTTPError(
            "http://x",
            409,
            "Conflict",
            {},
            io.BytesIO(b'{"error": "instance foo already exists"}'),
        )

        def _boom(req, timeout=None):
            raise err

        monkeypatch.setattr("urllib.request.urlopen", _boom)
        with pytest.raises(client.ApiError) as exc:
            client.post("http://127.0.0.1:8765", "/api/instances", {"title": "foo"})
        assert exc.value.status == 409
        assert "already exists" in exc.value.message

    def test_non_json_error_body_falls_back_to_status_reason(self, monkeypatch):
        # A proxy/500 page that isn't the usual {"error": ...} JSON: the message
        # degrades to "<code> <reason>" instead of leaking the HTML body.
        err = urllib.error.HTTPError(
            "http://x",
            500,
            "Internal Server Error",
            {},
            io.BytesIO(b"<html>Internal Server Error</html>"),
        )

        def _boom(req, timeout=None):
            raise err

        monkeypatch.setattr("urllib.request.urlopen", _boom)
        with pytest.raises(client.ApiError) as exc:
            client.get("http://127.0.0.1:8765", "/api/instances")
        assert exc.value.status == 500
        assert exc.value.message == "500 Internal Server Error"


class TestServerDiscovery:
    @pytest.fixture()
    def probed(self, monkeypatch):
        """Record probed base URLs; answer with the MindFlock config shape."""
        calls = []

        def _probe(base, timeout=1.0):
            calls.append(base)
            return {"default_program": "claude", "repo_root": "/x"}

        monkeypatch.setattr(client, "probe", _probe)
        return calls

    def test_explicit_flags_win_over_env(self, probed, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_PORT", "9999")
        assert client.discover("example.com", 9000) == "http://example.com:9000"
        assert probed == ["http://example.com:9000"]

    def test_env_port_used(self, probed, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_PORT", "9001")
        monkeypatch.delenv("MINDFLOCK_HOST", raising=False)
        assert client.discover() == "http://127.0.0.1:9001"

    def test_env_host_used(self, probed, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_HOST", "example.com")
        monkeypatch.delenv("MINDFLOCK_PORT", raising=False)
        assert client.discover() == "http://example.com:8765"
        assert probed == ["http://example.com:8765"]

    def test_default_probe(self, probed, monkeypatch):
        monkeypatch.delenv("MINDFLOCK_PORT", raising=False)
        monkeypatch.delenv("MINDFLOCK_HOST", raising=False)
        assert client.discover() == "http://127.0.0.1:8765"

    def test_no_server_raises_with_hint(self, monkeypatch):
        monkeypatch.delenv("MINDFLOCK_PORT", raising=False)
        monkeypatch.delenv("MINDFLOCK_HOST", raising=False)
        monkeypatch.setattr(client, "probe", lambda base, timeout=1.0: None)
        with pytest.raises(client.ServerNotFound) as exc:
            client.discover()
        assert "mindflock serve" in str(exc.value)

    def test_explicit_dead_address_named_in_error(self, monkeypatch):
        monkeypatch.setattr(client, "probe", lambda base, timeout=1.0: None)
        with pytest.raises(client.ServerNotFound) as exc:
            client.discover(None, 9000)
        assert "http://127.0.0.1:9000" in str(exc.value)

    def test_bad_env_port_rejected(self, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_PORT", "banana")
        with pytest.raises(client.ServerNotFound):
            client.discover()


class _FakeApi:
    """Canned client.get/post with call recording, for the command tests."""

    def __init__(self, monkeypatch, listings, post_result=None):
        self.listings = list(listings)  # successive GET /api/instances answers
        self.posts = []
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://127.0.0.1:8765")
        monkeypatch.setattr(client, "get", self._get)
        monkeypatch.setattr(client, "post", self._post)
        self._post_result = post_result if post_result is not None else {}
        # Make the ~15s readiness poll instant.
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    def _get(self, base, path, timeout=None):
        assert path == "/api/instances"
        return self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]

    def _post(self, base, path, payload=None, timeout=None):
        self.posts.append((path, payload))
        return self._post_result


class TestNewCommand:
    def test_payload_and_ready_report(self, monkeypatch, capsys, tmp_path):
        api = _FakeApi(
            monkeypatch,
            listings=[
                [],  # pre-create listing (auto-title)
                [{"title": "myrepo", "status": "running"}],  # readiness poll
            ],
            post_result={"title": "myrepo", "status": "loading"},
        )
        repo = tmp_path / "myrepo"
        repo.mkdir()
        assert cli.main(["new", str(repo), "-p", "fix the tests"]) == 0
        path, payload = api.posts[0]
        assert path == "/api/instances"
        assert payload["title"] == "myrepo"
        assert payload["repo_path"] == str(repo)
        assert payload["prompt"] == "fix the tests"
        assert "provisioned" not in payload
        out = capsys.readouterr().out
        assert "mindflock attach myrepo" in out
        assert "ready (status: running)" in out

    def test_auto_title_suffix_on_collision(self, monkeypatch, capsys, tmp_path):
        api = _FakeApi(
            monkeypatch,
            listings=[
                [{"title": "myrepo"}, {"title": "myrepo-2"}],
                [{"title": "myrepo-3", "status": "running"}],
            ],
            post_result={"title": "myrepo-3"},
        )
        repo = tmp_path / "myrepo"
        repo.mkdir()
        assert cli.main(["new", str(repo)]) == 0
        assert api.posts[0][1]["title"] == "myrepo-3"

    def test_provision_flags_forwarded(self, monkeypatch, capsys, tmp_path):
        api = _FakeApi(
            monkeypatch,
            listings=[[], [{"title": "r", "status": "running"}]],
            post_result={"title": "r"},
        )
        repo = tmp_path / "r"
        repo.mkdir()
        assert (
            cli.main(
                [
                    "new",
                    str(repo),
                    "--provision",
                    "--strategy",
                    "clone",
                    "-t",
                    "r",
                    "--program",
                    "codex",
                ]
            )
            == 0
        )
        payload = api.posts[0][1]
        assert payload["provisioned"] is True
        assert payload["workspace_strategy"] == "clone"
        assert payload["program"] == "codex"

    def test_repo_defaults_to_cwd(self, monkeypatch, capsys, tmp_path):
        api = _FakeApi(
            monkeypatch,
            listings=[[], [{"title": "proj", "status": "running"}]],
            post_result={"title": "proj"},
        )
        proj = tmp_path / "proj"
        proj.mkdir()
        monkeypatch.chdir(proj)
        assert cli.main(["new"]) == 0
        assert api.posts[0][1]["repo_path"] == os.path.realpath(str(proj))

    def test_vanished_session_reports_failure(self, monkeypatch, capsys, tmp_path):
        _FakeApi(
            monkeypatch,
            listings=[[], []],  # session gone after create → bg Start failed
            post_result={"title": "gone"},
        )
        repo = tmp_path / "gone"
        repo.mkdir()
        assert cli.main(["new", str(repo)]) == 1
        assert "failed to start" in capsys.readouterr().err

    def test_no_server_exits_one_with_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(
            client,
            "discover",
            lambda *a, **k: (_ for _ in ()).throw(client.ServerNotFound()),
        )
        assert cli.main(["new"]) == 1
        assert "mindflock serve" in capsys.readouterr().err


class TestNewCommandPolling:
    """The post-create readiness poll: a transient GET error is tolerated, and a
    session that never leaves 'loading' by the deadline reports 'still
    provisioning' (not a failure)."""

    def _wire(self, monkeypatch, get_fn, monotonic_values):
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://x")
        monkeypatch.setattr(client, "get", get_fn)
        monkeypatch.setattr(
            client,
            "post",
            lambda base, path, payload=None, timeout=None: {"title": "t"},
        )
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        seq = iter(monotonic_values)
        monkeypatch.setattr(cli.time, "monotonic", lambda: next(seq))

    def test_still_loading_after_deadline(self, monkeypatch, capsys, tmp_path):
        def _get(base, path, timeout=None):
            return [{"title": "t", "status": "loading"}]

        # First monotonic() sets the deadline; the loop-condition read is already
        # past it, so the poll body never runs and status stays "loading".
        self._wire(monkeypatch, _get, [0.0, 999.0])
        repo = tmp_path / "t"
        repo.mkdir()
        assert cli.main(["new", str(repo), "-t", "t"]) == 0
        assert "still provisioning" in capsys.readouterr().out

    def test_transient_get_error_during_poll_is_tolerated(
        self, monkeypatch, capsys, tmp_path
    ):
        calls = {"n": 0}

        def _get(base, path, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return []  # pre-create listing (auto-title)
            raise client.ClientError("transient network blip")

        # Enter the loop once (5 < 15), hit the error, then exit (999 >= 15).
        self._wire(monkeypatch, _get, [0.0, 5.0, 999.0])
        repo = tmp_path / "t"
        repo.mkdir()
        assert cli.main(["new", str(repo), "-t", "t"]) == 0
        assert calls["n"] == 2  # pre-create + one polled (errored) read
        assert "still provisioning" in capsys.readouterr().out


class TestLsCommand:
    _LISTING = [
        {
            "title": "fix-auth",
            "repo": "webapp",
            "status": "running",
            "activity": "working",
            "stage": "coding",
            "diff_stat": {"files": 3, "additions": 120, "deletions": 8},
            "tokens_cost": 0.42,
        },
        {
            "title": "old-server",
            "repo": "api",
            "status": "paused",
            "activity": "idle",
            "stage": "",
            # no diff_stat / tokens_cost → columns must be blank, not crash
        },
    ]

    def test_table_with_feature_detected_columns(self, monkeypatch, capsys):
        _FakeApi(monkeypatch, listings=[self._LISTING])
        assert cli.main(["ls"]) == 0
        out = capsys.readouterr().out
        lines = out.splitlines()
        assert lines[0].split() == [
            "TITLE",
            "REPO",
            "STATUS",
            "ACTIVITY",
            "STAGE",
            "DIFF",
            "COST",
        ]
        assert "+120 −8" in out
        assert "$0.42" in out
        row2 = [ln for ln in lines if ln.startswith("old-server")][0]
        assert "None" not in row2 and "+" not in row2 and "$" not in row2

    def test_json_flag_dumps_raw(self, monkeypatch, capsys):
        _FakeApi(monkeypatch, listings=[self._LISTING])
        assert cli.main(["ls", "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == self._LISTING

    def test_empty_listing_hint(self, monkeypatch, capsys):
        _FakeApi(monkeypatch, listings=[[]])
        assert cli.main(["ls"]) == 0
        assert "mindflock new" in capsys.readouterr().out


class TestAttachCommand:
    _LISTING = [
        {"title": "fix-auth", "tmux_name": "mindflock_fix-auth"},
        {"title": "fix-tests", "tmux_name": "mindflock_fix-tests"},
        {"title": "docs", "tmux_name": "mindflock_docs"},
    ]

    @pytest.fixture()
    def execvp_spy(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            cli.os, "execvp", lambda prog, argv: calls.append((prog, argv))
        )
        monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/tmux")
        # Under pytest stdout is captured (not a TTY); attach pre-checks it (L10).
        monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)
        return calls

    def test_exact_title_attaches(self, monkeypatch, execvp_spy):
        _FakeApi(monkeypatch, listings=[self._LISTING])
        cli.main(["attach", "fix-auth"])
        assert execvp_spy == [
            ("tmux", ["tmux", "attach-session", "-t", "mindflock_fix-auth"])
        ]

    def test_unambiguous_prefix_resolves(self, monkeypatch, execvp_spy):
        _FakeApi(monkeypatch, listings=[self._LISTING])
        cli.main(["attach", "do"])
        assert execvp_spy[0][1][-1] == "mindflock_docs"

    def test_ambiguous_prefix_lists_candidates(self, monkeypatch, execvp_spy, capsys):
        _FakeApi(monkeypatch, listings=[self._LISTING])
        assert cli.main(["attach", "fix"]) == 1
        err = capsys.readouterr().err
        assert "fix-auth" in err and "fix-tests" in err
        assert execvp_spy == []

    def test_unknown_title_errors(self, monkeypatch, execvp_spy, capsys):
        _FakeApi(monkeypatch, listings=[self._LISTING])
        assert cli.main(["attach", "nope"]) == 1
        assert "no session named" in capsys.readouterr().err

    def test_missing_tmux_binary(self, monkeypatch, capsys):
        _FakeApi(monkeypatch, listings=[self._LISTING])
        monkeypatch.setattr(cli.shutil, "which", lambda name: None)
        monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)
        assert cli.main(["attach", "docs"]) == 1
        assert "tmux not found" in capsys.readouterr().err

    def test_tmux_name_derived_when_field_missing(self, monkeypatch, execvp_spy):
        _FakeApi(monkeypatch, listings=[[{"title": "a b.c"}]])
        cli.main(["attach", "a b.c"])
        assert execvp_spy[0][1][-1] == "mindflock_ab_c"

    def test_non_tty_refused_with_scripting_hint(self, monkeypatch, capsys):
        """L10: attach inside a pipe/script fails fast with a friendly pointer
        (before any server discovery — no server needed to see the message)."""
        monkeypatch.setattr(cli, "_stdout_is_tty", lambda: False)
        monkeypatch.setattr(
            client,
            "discover",
            lambda *a, **k: pytest.fail("discover must not run for a non-TTY attach"),
        )
        assert cli.main(["attach", "docs"]) == 1
        err = capsys.readouterr().err
        assert "attach needs a real terminal" in err
        assert "mindflock ls --json" in err


class TestStdoutTty:
    def test_true_for_a_real_tty(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
        assert cli._stdout_is_tty() is True

    def test_false_when_isatty_raises(self, monkeypatch):
        # An exotic stdout replacement whose isatty() blows up must degrade to
        # False, never propagate (attach relies on this guard).
        class _Boom:
            def isatty(self):
                raise ValueError("no isatty here")

        monkeypatch.setattr(cli.sys, "stdout", _Boom())
        assert cli._stdout_is_tty() is False


class TestRmCommand:
    """L10: `mindflock rm TITLE [--yes]` → DELETE /api/instances/{title}."""

    _LISTING = [
        {"title": "fix-auth"},
        {"title": "fix-tests"},
        {"title": "docs"},
    ]

    @pytest.fixture()
    def deleted(self, monkeypatch):
        calls = []
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://127.0.0.1:8765")
        monkeypatch.setattr(
            client, "get", lambda base, path, timeout=None: self._LISTING
        )
        monkeypatch.setattr(
            client,
            "delete",
            lambda base, path, timeout=None: calls.append(path) or {"ok": True},
        )
        return calls

    def test_yes_flag_skips_prompt(self, deleted, monkeypatch, capsys):
        monkeypatch.setattr(
            "builtins.input", lambda *a: pytest.fail("must not prompt with --yes")
        )
        assert cli.main(["rm", "docs", "--yes"]) == 0
        assert deleted == ["/api/instances/docs"]
        out = capsys.readouterr().out
        assert "removed session docs" in out
        assert "worktree kept" in out

    def test_prompt_accepts_y(self, deleted, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        assert cli.main(["rm", "fix-auth"]) == 0
        assert deleted == ["/api/instances/fix-auth"]

    def test_prompt_declined_deletes_nothing(self, deleted, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        assert cli.main(["rm", "fix-auth"]) == 0
        assert deleted == []
        assert "aborted" in capsys.readouterr().out

    def test_prompt_eof_aborts_with_error(self, deleted, monkeypatch, capsys):
        def _eof(prompt):
            raise EOFError

        monkeypatch.setattr("builtins.input", _eof)
        assert cli.main(["rm", "fix-auth"]) == 1
        assert deleted == []
        assert "aborted" in capsys.readouterr().err

    def test_unambiguous_prefix_resolves(self, deleted, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "yes")
        assert cli.main(["rm", "do"]) == 0
        assert deleted == ["/api/instances/docs"]

    def test_ambiguous_prefix_lists_candidates(self, deleted, capsys):
        assert cli.main(["rm", "fix", "--yes"]) == 1
        err = capsys.readouterr().err
        assert "fix-auth" in err and "fix-tests" in err
        assert deleted == []

    def test_unknown_title_friendly_error(self, deleted, capsys):
        assert cli.main(["rm", "nope", "--yes"]) == 1
        err = capsys.readouterr().err
        assert "no session named" in err and "mindflock ls" in err
        assert deleted == []

    def test_no_server_exits_one_with_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(
            client,
            "discover",
            lambda *a, **k: (_ for _ in ()).throw(client.ServerNotFound()),
        )
        assert cli.main(["rm", "docs", "--yes"]) == 1
        assert "mindflock serve" in capsys.readouterr().err


class TestClientDelete:
    def test_delete_uses_http_delete(self, monkeypatch):
        seen = {}

        def _urlopen(req, timeout=None):
            seen["method"] = req.get_method()
            seen["url"] = req.full_url
            return _fake_response({"ok": True})

        monkeypatch.setattr("urllib.request.urlopen", _urlopen)
        assert client.delete("http://127.0.0.1:8765", "/api/instances/docs") == {
            "ok": True
        }
        assert seen["method"] == "DELETE"
        assert seen["url"].endswith("/api/instances/docs")


class TestOpenCommand:
    def test_posts_ide_endpoint(self, monkeypatch, capsys):
        api = _FakeApi(
            monkeypatch,
            listings=[[{"title": "fix-auth"}]],
            post_result={"ok": True, "opened_new": True},
        )
        assert cli.main(["open", "fix"]) == 0
        assert api.posts == [("/api/instances/fix-auth/ide", None)]
        assert "opened fix-auth" in capsys.readouterr().out

    def test_focus_message_when_already_open(self, monkeypatch, capsys):
        _FakeApi(
            monkeypatch,
            listings=[[{"title": "docs"}]],
            post_result={"ok": True, "opened_new": False},
        )
        assert cli.main(["open", "docs"]) == 0
        assert "focused" in capsys.readouterr().out

    def test_api_error_printed(self, monkeypatch, capsys):
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://x")
        monkeypatch.setattr(client, "get", lambda *a, **k: [{"title": "docs"}])

        def _post(*a, **k):
            raise client.ApiError(409, "workspace not ready")

        monkeypatch.setattr(client, "post", _post)
        assert cli.main(["open", "docs"]) == 1
        assert "workspace not ready" in capsys.readouterr().err


class TestEventFormatting:
    def test_status_change_line(self):
        line = cli._format_event(
            {
                "seq": 42,
                "event": "session.status_changed",
                "session": "my-title",
                "old": "loading",
                "new": "running",
                "ts": 0.0,
                "data": {},
            }
        )
        assert "session.status_changed" in line
        assert "my-title" in line
        assert "loading -> running" in line

    def test_data_payload_rendered(self):
        line = cli._format_event(
            {
                "event": "session.created",
                "session": "s",
                "new": "loading",
                "data": {"program": "claude"},
            }
        )
        assert '"program":"claude"' in line


class TestAutoTitle:
    """_auto_title sanitizes the repo basename into a tmux/branch-safe name and
    falls back to 'session' when nothing usable survives."""

    def test_special_chars_sanitized(self):
        assert cli._auto_title("/tmp/My Repo!", []) == "My-Repo"

    def test_root_path_falls_back_to_session(self):
        assert cli._auto_title("/", []) == "session"

    def test_all_special_sanitizes_to_session(self):
        assert cli._auto_title("/x/@@@", []) == "session"

    def test_collision_suffixes(self):
        assert cli._auto_title("proj", ["proj", "proj-2"]) == "proj-3"


class _FakeWs:
    """Context-manager websocket whose recv() replays a scripted sequence.

    Each item is ``("msg", raw_json)`` to return or ``("raise", exc)`` to raise
    (used to simulate the TimeoutError stop and a mid-stream ConnectionClosed)."""

    def __init__(self, script):
        self._script = list(script)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def recv(self, timeout=None):
        kind, payload = self._script.pop(0)
        if kind == "raise":
            raise payload
        return payload


class TestEventsCommand:
    """`mindflock events` — the missing-websockets guard, the backlog-then-stop
    (no --follow) path, the OSError failure, and the clean exit on a server
    close. No real socket is opened: a fake `websockets` is injected into
    sys.modules so the optional dependency and the network are both stubbed."""

    @pytest.fixture()
    def events_env(self, monkeypatch):
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://127.0.0.1:8765")

        class Ctl:
            ws = None
            connect_error = None
            url = None

        ctl = Ctl()
        ctl.ConnectionClosed = type("ConnectionClosed", (Exception,), {})

        def _connect(url):
            ctl.url = url
            if ctl.connect_error is not None:
                raise ctl.connect_error
            return ctl.ws

        sync_client = types.ModuleType("websockets.sync.client")
        sync_client.connect = _connect
        sync_mod = types.ModuleType("websockets.sync")
        sync_mod.client = sync_client
        exc_mod = types.ModuleType("websockets.exceptions")
        exc_mod.ConnectionClosed = ctl.ConnectionClosed
        pkg = types.ModuleType("websockets")

        monkeypatch.setitem(sys.modules, "websockets", pkg)
        monkeypatch.setitem(sys.modules, "websockets.sync", sync_mod)
        monkeypatch.setitem(sys.modules, "websockets.sync.client", sync_client)
        monkeypatch.setitem(sys.modules, "websockets.exceptions", exc_mod)
        return ctl

    def test_backlog_prints_then_stops_without_follow(self, events_env, capsys):
        env1 = {
            "event": "session.created",
            "session": "a",
            "new": "loading",
            "ts": 0.0,
        }
        env2 = {
            "event": "session.status_changed",
            "session": "a",
            "old": "loading",
            "new": "running",
            "ts": 0.0,
        }
        events_env.ws = _FakeWs(
            [
                ("msg", json.dumps(env1)),
                ("msg", json.dumps(env2)),
                ("raise", TimeoutError()),  # first quiet second -> stop
            ]
        )
        assert cli.main(["events"]) == 0
        out = capsys.readouterr().out
        assert "session.created" in out
        assert "loading -> running" in out
        # ws_url is otherwise untested: confirm the scheme swap end to end.
        assert events_env.url == "ws://127.0.0.1:8765/api/events"

    def test_missing_websockets_prints_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://127.0.0.1:8765")

        class _Block:
            def find_spec(self, name, path=None, target=None):
                if name == "websockets":
                    raise ModuleNotFoundError(
                        f"No module named '{name}'", name="websockets"
                    )
                return None

        for mod in list(sys.modules):
            if mod == "websockets" or mod.startswith("websockets."):
                monkeypatch.delitem(sys.modules, mod, raising=False)
        monkeypatch.setattr(sys, "meta_path", [_Block()] + sys.meta_path)
        assert cli.main(["events"]) == 1
        err = capsys.readouterr().err
        assert "websockets" in err
        # The remediation now points at the uv-tool reinstall form, not the
        # bare `uv sync --group web` (which only helps in a source checkout).
        assert 'uv tool install --force "mindflock[web]' in err

    def test_connect_oserror_reports_failure(self, events_env, capsys):
        events_env.connect_error = OSError("connection refused")
        assert cli.main(["events"]) == 1
        assert "event stream failed" in capsys.readouterr().err

    def test_server_close_during_follow_exits_clean(self, events_env, capsys):
        # A peer-initiated close mid-stream must be a clean exit, not a traceback.
        events_env.ws = _FakeWs([("raise", events_env.ConnectionClosed())])
        assert cli.main(["events", "--follow"]) == 0

    def test_keyboard_interrupt_exits_clean(self, events_env):
        # Ctrl-C while streaming is a normal stop, not an error exit.
        events_env.ws = _FakeWs([("raise", KeyboardInterrupt())])
        assert cli.main(["events", "--follow"]) == 0

    def test_ws_url_scheme_swap(self):
        assert (
            client.ws_url("http://127.0.0.1:8765", "/api/events")
            == "ws://127.0.0.1:8765/api/events"
        )
        assert client.ws_url("https://host", "/api/events") == "wss://host/api/events"


class TestVersionFlag:
    def test_version_prints_and_exits_zero(self, capsys):
        import backend

        with pytest.raises(SystemExit) as exc:
            cli.main(["--version"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert out.strip() == f"mindflock {backend.__version__}"

    def test_version_single_sourced_from_pyproject(self):
        """pyproject.toml [project] version is canonical; __version__ mirrors it."""
        import tomllib
        from pathlib import Path

        import backend

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        assert backend.__version__ == data["project"]["version"]


class TestUninstallCommand:
    """`mindflock uninstall` — the guards, not the filesystem work (that's
    covered end-to-end in test_uninstall.py)."""

    @pytest.fixture(autouse=True)
    def _no_server(self, monkeypatch):
        from backend import uninstall

        monkeypatch.setattr(uninstall, "server_is_running", lambda *a, **k: False)

    @pytest.fixture
    def empty_plan(self, monkeypatch):
        from backend import uninstall

        plan = uninstall.Plan()
        monkeypatch.setattr(uninstall, "build_plan", lambda: plan)
        return plan

    def test_refuses_while_a_server_is_running(self, monkeypatch, capsys):
        from backend import uninstall

        monkeypatch.setattr(uninstall, "server_is_running", lambda *a, **k: True)
        called = []
        monkeypatch.setattr(uninstall, "execute", lambda *a, **k: called.append(1))

        assert cli.main(["uninstall", "--yes"]) == 1
        assert not called, "must not touch anything while a server is up"
        assert "stop it first" in capsys.readouterr().err

    def test_dry_run_is_allowed_while_a_server_is_running(self, monkeypatch, capsys):
        """Previewing changes nothing, and is exactly when you'd want to look."""
        from backend import uninstall

        monkeypatch.setattr(uninstall, "server_is_running", lambda *a, **k: True)
        monkeypatch.setattr(uninstall, "build_plan", lambda: uninstall.Plan())

        assert cli.main(["uninstall", "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "a server is running" in out
        assert "nothing was changed" in out

    def test_prompt_declined_aborts(self, monkeypatch, capsys, empty_plan):
        from backend import uninstall

        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        called = []
        monkeypatch.setattr(uninstall, "execute", lambda *a, **k: called.append(1))

        assert cli.main(["uninstall"]) == 0
        assert not called
        assert "aborted" in capsys.readouterr().out

    def test_purge_prompt_names_the_stakes(self, monkeypatch, empty_plan):
        from backend import uninstall

        seen = []
        monkeypatch.setattr(
            "builtins.input", lambda prompt="": seen.append(prompt) or "n"
        )
        monkeypatch.setattr(uninstall, "execute", lambda *a, **k: uninstall.Report())

        cli.main(["uninstall", "--purge"])

        assert "usage history" in seen[0]

    def test_yes_skips_the_prompt_and_passes_flags(
        self, monkeypatch, capsys, empty_plan
    ):
        from backend import uninstall

        monkeypatch.setattr(
            "builtins.input", lambda prompt="": pytest.fail("should not prompt")
        )
        got = {}

        def fake_execute(plan, purge=False, dry_run=False, keep_worktrees=False):
            got.update(purge=purge, dry_run=dry_run, keep_worktrees=keep_worktrees)
            return uninstall.Report()

        monkeypatch.setattr(uninstall, "execute", fake_execute)

        assert cli.main(["uninstall", "--yes", "--purge", "--keep-worktrees"]) == 0
        assert got == {"purge": True, "dry_run": False, "keep_worktrees": True}

    def test_prints_the_final_uv_step(self, monkeypatch, capsys, empty_plan):
        from backend import uninstall

        monkeypatch.setattr(uninstall, "execute", lambda *a, **k: uninstall.Report())

        cli.main(["uninstall", "--yes"])

        assert "uv tool uninstall mindflock" in capsys.readouterr().out

    def test_dry_run_does_not_print_the_uv_step(self, monkeypatch, capsys, empty_plan):
        from backend import uninstall

        monkeypatch.setattr(uninstall, "execute", lambda *a, **k: uninstall.Report())

        cli.main(["uninstall", "--dry-run"])

        assert "uv tool uninstall" not in capsys.readouterr().out

    def test_errors_produce_a_nonzero_exit(self, monkeypatch, capsys, empty_plan):
        from backend import uninstall

        report = uninstall.Report()
        report.failed("could not delete /x")
        monkeypatch.setattr(uninstall, "execute", lambda *a, **k: report)

        assert cli.main(["uninstall", "--yes"]) == 1
        assert "could not delete /x" in capsys.readouterr().err

    def test_plan_warnings_go_to_stderr(self, monkeypatch, capsys):
        from backend import uninstall

        plan = uninstall.Plan()
        plan.warnings.append("could not read state.json")
        monkeypatch.setattr(uninstall, "build_plan", lambda: plan)
        monkeypatch.setattr(uninstall, "execute", lambda *a, **k: uninstall.Report())

        cli.main(["uninstall", "--yes"])

        assert "could not read state.json" in capsys.readouterr().err
