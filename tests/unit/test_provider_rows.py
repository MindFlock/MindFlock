"""Settings → Agent providers rows: the backend they read.

* ``patch_toml_tables`` / ``PUT /api/providers/{name}`` keep every key and
  table the form never sent (byte-for-byte but the edited key),
* ``PUT /api/providers/{name}/binary-path`` writes / clears this device's
  ``coding_cli.binary_paths[name]`` — for built-ins too — and sync never
  carries it,
* a custom TOML that fails to load is reported on ``/api/providers/status``,
* the doctor probes the login of every provider some setting points at.
"""

from __future__ import annotations

import os
import tomllib

import pytest
from fastapi.testclient import TestClient

from backend import doctor, providers
from backend.config import settings as S
from backend.providers import config as provider_config
from backend.providers import claude as claude_provider


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_PROVIDERS_DIR", str(tmp_path / "providers"))
    for var in list(os.environ):
        if var.startswith("MINDFLOCK_PROVIDER_BIN_"):
            monkeypatch.delenv(var, raising=False)
    S.invalidate()
    providers.rebuild_registry()
    yield tmp_path
    S.invalidate()
    providers.rebuild_registry()


@pytest.fixture()
def client(isolated):
    from backend.web.server import app

    with TestClient(app) as c:
        yield c


RICH_TOML = """# hand-written: every table the form does not know
[provider]
name = "rich"
program = ["rich", "rich-cli"]   # aliases
command = "rich"

[launch]
args = [
  "--yes",      # keep
  "--fast",
]
resume_flag = "--continue"
prompt_arg = "{prompt}"
oneshot_args = ["-p", "{prompt}"]
effort_args = ["--effort", "{level}"]
effort_levels = ["low", "medium", "high"]
effort_ultra_level = "ultra"

[usage]
window_kind = "rolling"
window_hours = 5.0
limit_patterns = ["limit reached"]

[activity]
hooks_file = "~/.rich/markers.toml"
red_zone_guard = true
[[activity.events]]
event = "Start"
state = "working"

[connect]
install_hint = "curl -fsSL https://example.com/install.sh | bash"
auth_files = ["~/.rich/auth.json"]
login_command = "rich login"

[peer]
egress = ["api.example.com:443"]
[peer.mcp_file]
name = "mcp.json"
content = "{}"
"""


# --------------------------------------------------------------------------- #
# patch_toml_tables
# --------------------------------------------------------------------------- #
class TestPatchTomlTables:
    def test_edit_of_args_changes_only_that_line(self):
        out = provider_config.patch_toml_tables(
            RICH_TOML, {"launch": {"args": ["--yes", "--slow"]}}
        )
        expected = RICH_TOML.replace(
            'args = [\n  "--yes",      # keep\n  "--fast",\n]\n',
            'args = ["--yes", "--slow"]\n',
        )
        assert out == expected  # byte-for-byte but the edited key
        parsed = tomllib.loads(out)
        assert parsed["launch"]["oneshot_args"] == ["-p", "{prompt}"]
        assert parsed["usage"]["window_hours"] == 5.0
        assert parsed["activity"]["events"][0]["event"] == "Start"
        assert parsed["connect"]["login_command"] == "rich login"
        assert parsed["peer"]["mcp_file"]["name"] == "mcp.json"

    def test_single_line_key_keeps_its_trailing_comment(self):
        out = provider_config.patch_toml_tables(
            RICH_TOML, {"provider": {"program": "rich"}}
        )
        assert 'program = "rich"   # aliases\n' in out
        assert out == RICH_TOML.replace(
            'program = ["rich", "rich-cli"]   # aliases', 'program = "rich"   # aliases'
        )

    def test_none_removes_a_key_and_new_keys_land_in_their_table(self):
        out = provider_config.patch_toml_tables(
            RICH_TOML,
            {
                "launch": {"resume_flag": None, "skip_perms_flag": "--yolo"},
                "classify": {"idle_pattern": "> "},
            },
        )
        parsed = tomllib.loads(out)
        assert "resume_flag" not in parsed["launch"]
        assert parsed["launch"]["skip_perms_flag"] == "--yolo"
        assert parsed["classify"]["idle_pattern"] == "> "
        # The new launch key sits inside [launch], before the blank line
        # that separates it from [usage]; the new table is appended last.
        assert (
            'effort_ultra_level = "ultra"\nskip_perms_flag = "--yolo"\n\n[usage]' in out
        )
        assert out.endswith('content = "{}"\n\n[classify]\nidle_pattern = "> "\n')

    def test_empty_patch_is_identity(self):
        assert provider_config.patch_toml_tables(RICH_TOML, {"launch": {}}) == RICH_TOML
        assert provider_config.patch_toml_tables(RICH_TOML, {}) == RICH_TOML

    def test_a_hash_inside_a_string_is_not_a_comment(self):
        src = '[launch]\nargs = ["--tag=#1"]\n'
        out = provider_config.patch_toml_tables(src, {"launch": {"args": ["--x"]}})
        assert out == '[launch]\nargs = ["--x"]\n'


class TestPutPreservesTheToml:
    def test_put_args_keeps_every_other_table(self, client, isolated):
        d = isolated / "providers"
        d.mkdir()
        (d / "rich.toml").write_text(RICH_TOML, encoding="utf-8")
        providers.rebuild_registry()
        r = client.put(
            "/api/providers/rich",
            json={
                "name": "rich",
                "program": "rich rich-cli",
                "binary_path": "",
                "launch_args": ["--yes", "--slow"],
                "resume_flag": "--continue",
                "skip_perms_flag": "",
            },
        )
        assert r.status_code == 200, r.text
        text = (d / "rich.toml").read_text(encoding="utf-8")
        assert text == RICH_TOML.replace(
            'args = [\n  "--yes",      # keep\n  "--fast",\n]\n',
            'args = ["--yes", "--slow"]\n',
        )
        cfg = providers.get("rich").cfg
        assert cfg.launch_args == ("--yes", "--slow")
        assert cfg.prompt_arg == "{prompt}"
        assert cfg.oneshot_args == ("-p", "{prompt}")
        assert cfg.effort_levels == ("low", "medium", "high")
        assert cfg.usage_window_hours == 5.0
        assert cfg.activity_hook_events == (("Start", "working"),)
        assert cfg.login_command == "rich login"
        assert cfg.peer_mcp_file == ("mcp.json", "{}")

    def test_put_on_a_broken_toml_refuses_instead_of_rewriting(self, client, isolated):
        d = isolated / "providers"
        d.mkdir()
        (d / "broken.toml").write_text("[provider\nname = ", encoding="utf-8")
        r = client.put("/api/providers/broken", json={"launch_args": ["--x"]})
        assert r.status_code == 409
        assert "broken.toml" in r.json()["error"]
        assert (d / "broken.toml").read_text(encoding="utf-8") == "[provider\nname = "


# --------------------------------------------------------------------------- #
# parse failures on /api/providers/status
# --------------------------------------------------------------------------- #
class TestLoadErrors:
    def test_status_lists_the_file_and_the_error(self, client, isolated, caplog):
        d = isolated / "providers"
        d.mkdir()
        (d / "bad.toml").write_text("[provider\nname = ", encoding="utf-8")
        (d / "noname.toml").write_text("[provider]\nprogram = 'x'\n", encoding="utf-8")
        (d / "good.toml").write_text('[provider]\nname = "good"\n', encoding="utf-8")
        with caplog.at_level("WARNING", logger="backend.providers.config"):
            providers.rebuild_registry()
        assert any("bad.toml" in rec.getMessage() for rec in caplog.records)
        body = client.get("/api/providers/status").json()
        errors = {e["file"]: e["error"] for e in body["errors"]}
        assert set(errors) == {"bad.toml", "noname.toml"}
        assert "TOMLDecodeError" in errors["bad.toml"]
        assert "name" in errors["noname.toml"]
        assert "good" in {p["name"] for p in body["providers"]}

    def test_status_carries_sign_in_and_override_fields(self, client, monkeypatch):
        import backend.web.addons.settings as settings_addon

        monkeypatch.setattr(settings_addon.shutil, "which", lambda b: None)
        provs = {
            p["name"]: p
            for p in client.get("/api/providers/status").json()["providers"]
        }
        assert (
            provs["codex"]["has_login"] is True and provs["codex"]["auth_probe"] is True
        )
        assert provs["aider"]["has_login"] is False  # API keys: "no sign-in needed"
        assert provs["claude"]["binary_override"] == ""
        assert provs["claude"]["binary_override_env"] == ""


# --------------------------------------------------------------------------- #
# PUT /api/providers/{name}/binary-path
# --------------------------------------------------------------------------- #
class TestBinaryPath:
    def test_write_then_clear_for_a_builtin(self, client, tmp_path):
        exe = tmp_path / "claude"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        r = client.put("/api/providers/claude/binary-path", json={"path": str(exe)})
        assert r.status_code == 200, r.text
        assert r.json()["warning"] == ""
        assert r.json()["status"]["binary_override"] == str(exe)
        assert r.json()["status"]["installed"] is True
        assert S.load_settings().coding_cli.binary_paths == {"claude": str(exe)}
        assert provider_config.resolve_provider_binary("claude") == str(exe)
        r = client.put("/api/providers/claude/binary-path", json={"path": ""})
        assert r.status_code == 200
        assert S.load_settings().coding_cli.binary_paths == {}
        assert r.json()["status"]["binary_override"] == ""

    def test_missing_file_is_saved_with_a_warning(self, client, tmp_path):
        r = client.put(
            "/api/providers/codex/binary-path", json={"path": str(tmp_path / "nope")}
        )
        assert r.status_code == 200
        assert "not an executable file" in r.json()["warning"]
        assert S.load_settings().coding_cli.binary_paths["codex"].endswith("nope")

    def test_env_override_is_named(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_PROVIDER_BIN_CODEX", "/env/codex")
        r = client.put("/api/providers/codex/binary-path", json={"path": "/x/codex"})
        assert "MINDFLOCK_PROVIDER_BIN_CODEX" in r.json()["warning"]
        assert (
            r.json()["status"]["binary_override_env"] == "MINDFLOCK_PROVIDER_BIN_CODEX"
        )

    def test_unknown_provider_is_404(self, client):
        assert (
            client.put(
                "/api/providers/nope/binary-path", json={"path": "/x"}
            ).status_code
            == 404
        )
        assert (
            client.put(
                "/api/providers/generic/binary-path", json={"path": "/x"}
            ).status_code
            == 404
        )

    def test_binary_paths_never_sync(self, client, tmp_path):
        from backend.web.core import settings_sync

        assert "coding_cli.binary_paths" not in settings_sync.paths()
        assert "binary_paths" in settings_sync.LOCAL["coding_cli"]
        client.put(
            "/api/providers/claude/binary-path", json={"path": str(tmp_path / "c")}
        )
        raw, _failed = settings_sync._snapshot()
        assert not [k for k in raw if k.startswith("coding_cli.binary_paths")]
        assert (
            "coding_cli.default_provider" in raw
        )  # the snapshot did look at coding_cli


# --------------------------------------------------------------------------- #
# Test agent CLI with a provider picked
# --------------------------------------------------------------------------- #
class TestTestAgentPicker:
    def test_probes_the_named_provider(self, client, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda b: None)
        r = client.post("/api/settings/test/agent", json={"provider": "aider"}).json()
        assert r["cli"]["id"] == "agent-cli" and r["cli"]["provider"] == "aider"
        assert r["auth"]["id"] == "agent-auth" and r["auth"]["provider"] == "aider"
        assert "aider" in r["cli"]["label"]

    def test_no_body_still_probes_the_default(self, client, monkeypatch):
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "codex")
        monkeypatch.setattr(doctor.shutil, "which", lambda b: None)
        r = client.post("/api/settings/test/agent").json()
        assert r["cli"]["provider"] == "codex"


# --------------------------------------------------------------------------- #
# Doctor: every referenced provider gets a login row
# --------------------------------------------------------------------------- #
def _which(table):
    return lambda name: table.get(name)


class TestReferencedAgents:
    @pytest.fixture(autouse=True)
    def _setup(self, isolated, monkeypatch):
        monkeypatch.setenv("HOME", str(isolated))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(claude_provider, "_keychain_login_evidence", lambda: False)
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "claude")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: name)
        S.save_settings(
            S.Settings.from_dict(
                {
                    "coding_cli": {"assistant_provider": "codex"},
                    "engine": {"agent": "claude"},  # the default: covered already
                    "ticketing": {
                        "sources": [
                            {"id": "jira1", "provider": "jira", "agent": "aider"},
                            {"id": "sc", "provider": "shortcut", "agent": "codex"},
                            {"id": "lin", "provider": "linear"},  # inherits
                        ]
                    },
                    "github": {
                        "agent": "codex",
                        "issue_agent": "aider",
                        "repo_settings": {"o/r": {"agent": "goose"}},
                        "issue_repo_settings": {"o/r": {"agent": "codex"}},
                    },
                }
            )
        )

    def test_referenced_agents_collects_roles_and_skips_the_default(self):
        assert doctor.referenced_agents() == {
            "codex": [
                "Assistant",
                "ticket source sc",
                "PR review",
                "issues of o/r",
            ],
            "aider": ["ticket source jira1", "issue handling"],
            "goose": ["PR review of o/r"],
        }

    def test_one_row_per_provider_with_its_own_verdict(self, monkeypatch):
        monkeypatch.setattr(
            doctor.shutil,
            "which",
            _which({"codex": "/usr/bin/codex", "aider": "/usr/bin/aider"}),
        )
        rows = {c.id: c for c in doctor.check_referenced_agents_auth()}
        assert set(rows) == {"agent-auth-aider", "agent-auth-codex", "agent-auth-goose"}
        codex = rows["agent-auth-codex"]
        assert codex.status == "warn" and codex.cmd == "codex login"
        assert codex.provider == "codex"
        assert (
            codex.label
            == "agent auth (codex — Assistant, ticket source sc, PR review, issues of o/r)"
        )
        aider = rows["agent-auth-aider"]
        assert aider.status == "warn" and aider.cmd == ""  # API keys: nothing to run
        assert rows["agent-auth-goose"].status == "info"  # declares no sources

    def test_default_row_is_unchanged_and_the_rows_reach_run_checks(self, monkeypatch):
        monkeypatch.setattr(
            doctor.shutil, "which", _which({"claude": "/usr/bin/claude"})
        )
        assert doctor.check_agent_auth().id == "agent-auth"
        assert (
            doctor.CHECKS_BY_ID["agent-auth-referenced"]
            is doctor.check_referenced_agents_auth
        )
        ids = [c.id for c in doctor.check_referenced_agents_auth()]
        assert ids == ["agent-auth-aider", "agent-auth-codex", "agent-auth-goose"]

    def test_nothing_referenced_means_no_rows(self):
        S.save_settings(S.Settings.from_dict({}))
        assert doctor.referenced_agents() == {}
        assert doctor.check_referenced_agents_auth() == []
