"""Contract tests for the Settings addon (/api/settings + /api/providers*)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import providers
from backend.config import settings as S


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_PROVIDERS_DIR", str(tmp_path / "providers"))
    S.invalidate()
    providers.rebuild_registry()
    from backend.web.server import app

    with TestClient(app) as c:
        yield c
    S.invalidate()
    providers.rebuild_registry()


class TestSettingsEndpoint:
    def test_addon_in_manifest(self, client):
        ids = [a["id"] for a in client.get("/api/addons").json()["addons"]]
        assert "settings" in ids

    def test_get_empty(self, client):
        s = client.get("/api/settings").json()["settings"]
        # secret groups always present (masked), others absent when empty
        assert s["github"]["token"] == ""

    def test_post_and_readback(self, client):
        client.post(
            "/api/settings",
            json={
                "repository": {"url": "git@x:me/r.git"},
                "github": {"base_branch": "main"},
            },
        )
        s = client.get("/api/settings").json()["settings"]
        assert s["repository"]["url"] == "git@x:me/r.git"
        assert s["github"]["base_branch"] == "main"

    def test_secret_masked_on_read(self, client):
        client.post("/api/settings", json={"github": {"token": "gh_secret"}})
        s = client.get("/api/settings").json()["settings"]
        assert s["github"]["token"] == "•••set"  # never the raw value
        assert "gh_secret" not in str(s)

    def test_empty_secret_keeps_existing(self, client):
        client.post("/api/settings", json={"github": {"token": "gh_secret"}})
        # A form re-save sends the mask/"" back — must not wipe the token.
        client.post("/api/settings", json={"github": {"token": ""}})
        assert S.load_settings().github.token == "gh_secret"
        client.post("/api/settings", json={"github": {"token": "•••set"}})
        assert S.load_settings().github.token == "gh_secret"

    def test_partial_update_isolation(self, client):
        client.post("/api/settings", json={"repository": {"url": "u"}})
        client.post("/api/settings", json={"github": {"repos": ["o/r"]}})
        s = client.get("/api/settings").json()["settings"]
        assert s["repository"]["url"] == "u"  # untouched by the github POST
        assert s["github"]["repos"] == ["o/r"]

    def test_uninstalled_default_provider_rejected(self, client, monkeypatch):
        # The default agent provider must be an installed CLI; setting a missing
        # one is a clean 4xx error, not a 500, and never mutates the store.
        import backend.web.addons.settings as settings_addon

        monkeypatch.setattr(settings_addon, "_provider_installed", lambda name: False)
        r = client.post(
            "/api/settings", json={"coding_cli": {"default_provider": "codex"}}
        )
        assert r.status_code == 400
        assert "codex" in r.json()["error"]
        assert S.load_settings().coding_cli.default_provider == ""  # settings unchanged

    def test_installed_default_provider_accepted(self, client, monkeypatch):
        import backend.web.addons.settings as settings_addon

        monkeypatch.setattr(settings_addon, "_provider_installed", lambda name: True)
        r = client.post(
            "/api/settings", json={"coding_cli": {"default_provider": "codex"}}
        )
        assert r.status_code == 200
        assert S.load_settings().coding_cli.default_provider == "codex"


class TestProviderCrudApi:
    def test_list_excludes_generic(self, client):
        names = [
            p["name"] for p in client.get("/api/providers/manage").json()["providers"]
        ]
        assert "claude" in names and "aider" in names
        assert "generic" not in names

    def test_builtins_marked_readonly(self, client):
        provs = {
            p["name"]: p
            for p in client.get("/api/providers/manage").json()["providers"]
        }
        assert provs["claude"]["editable"] is False
        assert provs["aider"]["source"] == "builtin"

    def test_create_then_appears(self, client):
        r = client.post(
            "/api/providers",
            json={"name": "mycli", "program": "mycli", "binary_path": "/opt/mycli"},
        )
        assert r.status_code == 200
        names = [
            p["name"] for p in client.get("/api/providers/manage").json()["providers"]
        ]
        assert "mycli" in names

    def test_create_with_saved_launch_args(self, client):
        r = client.post(
            "/api/providers",
            json={
                "name": "autocli",
                "program": "autocli",
                "launch_args": ["--dangerously-skip-permissions"],
            },
        )
        assert r.status_code == 200
        assert r.json()["provider"]["launch_args"] == ["--dangerously-skip-permissions"]
        p = providers.get("autocli")
        assert p.build_launch_command(providers.LaunchContext(program="autocli")) == (
            "autocli --dangerously-skip-permissions"
        )

    def test_create_rejects_invalid_launch_args(self, client):
        r = client.post(
            "/api/providers",
            json={
                "name": "badargs",
                "program": "badargs",
                "launch_args": "--not-a-list",
            },
        )
        assert r.status_code == 400
        assert "launch args" in r.json()["error"]

    def test_provider_toml_escapes_embedded_quotes(self, client):
        # Regression: hand-rolled `"%s"` interpolation produced invalid TOML when
        # a value contained a double-quote (TOML-injection/escaping bug). The
        # rendered doc must parse and preserve the quote-bearing values verbatim.
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover — py<3.11
            import tomli as tomllib
        from backend.web.addons.settings import _provider_toml

        body = {
            "name": "quotecli",
            "program": 'my "quoted" cli',
            "skip_perms_flag": '--say="hi"',
            "launch_args": ['--label="a b"'],
            "trust_patterns": ['do you "trust"?'],
            "idle_pattern": 'press "enter"',
        }
        doc = _provider_toml(body)
        parsed = tomllib.loads(doc)  # must not raise on the embedded quotes
        assert parsed["provider"]["program"] == 'my "quoted" cli'
        assert parsed["launch"]["skip_perms_flag"] == '--say="hi"'
        assert parsed["launch"]["args"] == ['--label="a b"']
        assert parsed["classify"]["trust_patterns"] == ['do you "trust"?']
        assert parsed["classify"]["idle_pattern"] == 'press "enter"'

    def test_provider_toml_omits_args_when_empty(self, client):
        from backend.web.addons.settings import _provider_toml

        doc = _provider_toml({"name": "noargs", "program": "noargs"})
        assert "args = [" not in doc

    def test_create_rejects_builtin_name(self, client):
        assert client.post("/api/providers", json={"name": "claude"}).status_code == 400

    def test_create_rejects_bad_name(self, client):
        assert (
            client.post("/api/providers", json={"name": "Bad Name!"}).status_code == 400
        )

    def test_create_duplicate_conflict(self, client):
        client.post("/api/providers", json={"name": "dup", "program": "dup"})
        assert client.post("/api/providers", json={"name": "dup"}).status_code == 409

    def test_update_user_provider(self, client):
        client.post("/api/providers", json={"name": "edit", "program": "edit"})
        r = client.put("/api/providers/edit", json={"binary_path": "/new/edit"})
        assert r.status_code == 200
        assert r.json()["provider"]["binary_path"] == "/new/edit"

    def test_update_rejects_builtin(self, client):
        assert (
            client.put("/api/providers/claude", json={"binary_path": "/x"}).status_code
            == 400
        )

    def test_delete_user_provider(self, client):
        client.post("/api/providers", json={"name": "gone", "program": "gone"})
        assert client.delete("/api/providers/gone").json()["deleted"] is True
        names = [
            p["name"] for p in client.get("/api/providers/manage").json()["providers"]
        ]
        assert "gone" not in names

    def test_delete_rejects_builtin(self, client):
        assert client.delete("/api/providers/codex").status_code == 400
