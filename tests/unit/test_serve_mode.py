"""The persisted serve mode (Settings → Mobile tailscale toggle).

Covers the three layers of the feature:

* the ``general.serve_mode`` settings field (round-trip, update, clear),
* :mod:`backend.web.run` falling back to it when neither the CLI nor
  ``CS_WEB_MODE`` picks a mode (explicit choices still win),
* ``/api/mobile``'s payload/note reflecting a saved-but-not-live choice so the
  UI can offer the one-click restart.
"""

from __future__ import annotations

import pytest

from backend.config import settings as S


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store):
    """Delegate to the shared settings-store isolation (tests/conftest.py)."""


class TestServeModeSetting:
    def test_default_is_empty(self):
        assert S.load_settings().general.serve_mode == ""

    def test_round_trip(self):
        s = S.Settings()
        s.general.serve_mode = "tailscale"
        S.save_settings(s)
        S.invalidate()
        assert S.load_settings().general.serve_mode == "tailscale"

    def test_update_and_clear(self):
        S.update_settings(general={"serve_mode": "tailscale"})
        assert S.load_settings().general.serve_mode == "tailscale"
        S.update_settings(general={"serve_mode": ""})
        assert S.load_settings().general.serve_mode == ""


class TestRunModeResolution:
    @pytest.fixture()
    def run_mod(self, monkeypatch):
        from backend import doctor
        from backend.doctor import Check
        from backend.web import run as run_mod

        # Neutralize everything main() does besides resolving mode/host.
        monkeypatch.setenv("CS_WEB_MODE", "")
        monkeypatch.setenv("UVICORN_PORT", "8765")
        monkeypatch.setattr(run_mod, "_port_squatter", lambda host, port: "")
        # …including the preflight daemon thread: with a tmp settings store every
        # call looks like a first run, and the report walks the real doctor and
        # scans the developer's home for repo suggestions on a thread that
        # outlives the test.
        monkeypatch.setattr(run_mod, "_is_onboarded", lambda: True)
        _ok = Check(id="stub", label="stub", status="ok")
        for _name in ("check_git", "check_tmux", "check_agent_cli"):
            monkeypatch.setattr(doctor, _name, lambda: _ok)
        return run_mod

    def _captured_host(self, monkeypatch, run_mod, argv):
        import uvicorn

        captured = {}
        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
        run_mod.main(argv)
        return captured["host"]

    def test_norm_mode(self, run_mod):
        assert run_mod._norm_mode(None) == ""
        assert run_mod._norm_mode("  ") == ""
        assert run_mod._norm_mode("tailscale") == "tailscale"
        assert run_mod._norm_mode("TS") == "tailscale"
        assert run_mod._norm_mode("localhost") == "local"
        # unknown values map to the safe bind, never to exposure
        assert run_mod._norm_mode("bogus") == "local"

    def test_settings_mode_used_when_nothing_explicit(self, monkeypatch, run_mod):
        S.update_settings(general={"serve_mode": "tailscale"})
        assert self._captured_host(monkeypatch, run_mod, []) == "0.0.0.0"

    def test_default_is_local_without_setting(self, monkeypatch, run_mod):
        assert self._captured_host(monkeypatch, run_mod, []) == "127.0.0.1"

    def test_cli_arg_beats_setting(self, monkeypatch, run_mod):
        S.update_settings(general={"serve_mode": "tailscale"})
        assert self._captured_host(monkeypatch, run_mod, ["local"]) == "127.0.0.1"

    def test_env_beats_setting(self, monkeypatch, run_mod):
        S.update_settings(general={"serve_mode": "local"})
        monkeypatch.setenv("CS_WEB_MODE", "tailscale")
        assert self._captured_host(monkeypatch, run_mod, []) == "0.0.0.0"

    def test_bogus_setting_falls_back_to_local(self, monkeypatch, run_mod):
        S.update_settings(general={"serve_mode": "bogus"})
        assert self._captured_host(monkeypatch, run_mod, []) == "127.0.0.1"


class TestMobileInfoServeMode:
    @pytest.fixture()
    def server(self, monkeypatch):
        from backend.web import server

        monkeypatch.setenv("CS_WEB_MODE", "local")
        monkeypatch.delenv("MINDFLOCK_AUTH_TOKEN", raising=False)
        return server

    def test_local_mode_pending_restart_note(self, server):
        S.update_settings(general={"serve_mode": "tailscale"})
        info = server._mobile_info()
        assert info["local_only"] is True
        assert info["serve_mode"] == "tailscale"
        assert "restart" in info["note"].lower()

    def test_local_mode_toggle_off_note(self, server):
        info = server._mobile_info()
        assert info["serve_mode"] == ""
        assert "turn on tailscale mode" in info["note"].lower()

    def test_tailscale_mode_reports_setting(self, server, monkeypatch):
        monkeypatch.setenv("CS_WEB_MODE", "tailscale")
        monkeypatch.setattr(
            server, "_tailscale_info", lambda: ("myhost.tail.net", "100.1.2.3")
        )
        monkeypatch.setattr(server, "_tailscale_serves_port", lambda port: False)
        S.update_settings(general={"serve_mode": "tailscale"})
        info = server._mobile_info()
        assert info["local_only"] is False
        assert info["serve_mode"] == "tailscale"
        assert info["note"] is None
