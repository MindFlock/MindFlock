"""Auth profiles (multiple Claude accounts / OpenRouter keys) — the overlay
contract, the settings store round-trip, the session field tri-state, and the
/api/settings/auth-profiles surface.

The load-bearing invariant everywhere: **no profile must mean exactly the
prior behaviour** — ``({}, ())`` overlays, byte-identical launch scripts (the
golden tests in test_launch_parity.py pin that side), and absent JSON keys.
"""

from __future__ import annotations

import io
import json
import urllib.request

import pytest
from fastapi.testclient import TestClient

from backend import providers
from backend.config import settings as S
from backend.providers import auth_profiles as ap
from backend.providers import launch_script
from backend.session.storage import InstanceData


@pytest.fixture(autouse=True)
def _fresh_settings(tmp_path, monkeypatch):
    """Isolated settings store + HOME (account dirs derive from ~/.mindflock)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINDFLOCK_AUTH_PROFILE", raising=False)
    S.invalidate()
    yield
    S.invalidate()


def _profile(**kw) -> ap.AuthProfileConfig:
    return ap.AuthProfileConfig(**{"id": "p", **kw})


# --------------------------------------------------------------------------- #
# overlay_for: the (env, launch_args) matrix
# --------------------------------------------------------------------------- #
def test_no_profile_is_a_no_op():
    assert ap.overlay_for("claude", None) == ({}, ())


def test_account_claude_sets_config_dir_explicit():
    p = _profile(kind="account", provider="claude", config_dir="/tmp/work-claude")
    env, args = ap.overlay_for("claude", p)
    assert env == {"CLAUDE_CONFIG_DIR": "/tmp/work-claude"}
    assert args == ()


def test_account_claude_default_dir_under_mindflock(tmp_path):
    p = _profile(id="work", kind="account", provider="claude")
    env, _ = ap.overlay_for("claude", p)
    assert env["CLAUDE_CONFIG_DIR"] == str(
        tmp_path / ".mindflock" / "accounts" / "work"
    )


def test_account_blank_provider_means_claude():
    p = _profile(kind="account", config_dir="/tmp/d")
    assert ap.overlay_for("claude", p)[0] == {"CLAUDE_CONFIG_DIR": "/tmp/d"}


def test_account_model_pin_rides_along():
    p = _profile(kind="account", config_dir="/tmp/d", model="claude-sonnet-4-5")
    env, _ = ap.overlay_for("claude", p)
    assert env["ANTHROPIC_MODEL"] == "claude-sonnet-4-5"


def test_account_codex_uses_codex_home():
    p = _profile(kind="account", provider="codex", config_dir="/tmp/codex-work")
    assert ap.overlay_for("codex", p) == ({"CODEX_HOME": "/tmp/codex-work"}, ())


def test_account_does_not_leak_onto_other_clis():
    p = _profile(kind="account", provider="claude", config_dir="/tmp/d")
    assert ap.overlay_for("codex", p) == ({}, ())
    assert ap.overlay_for("aider", p) == ({}, ())


def test_api_key_claude():
    p = _profile(kind="api_key", provider="claude", api_key="sk-ant-x")
    assert ap.overlay_for("claude", p) == ({"ANTHROPIC_API_KEY": "sk-ant-x"}, ())


def test_api_key_codex_with_model_flag():
    p = _profile(kind="api_key", provider="codex", api_key="sk-x", model="gpt-5.2")
    env, args = ap.overlay_for("codex", p)
    assert env == {"OPENAI_API_KEY": "sk-x"}
    assert args == ("-m", "gpt-5.2")


def test_api_key_without_key_is_a_no_op():
    p = _profile(kind="api_key", provider="claude")
    assert ap.overlay_for("claude", p) == ({}, ())


def test_openrouter_claude_uses_anthropic_gateway_env():
    p = _profile(kind="openrouter", api_key="sk-or-x", model="anthropic/claude-4.5")
    env, args = ap.overlay_for("claude", p)
    assert env == {
        # The Anthropic SDK appends /v1/… itself, so the claude route strips
        # the OpenAI-style /v1 the stored base carries (…/api/v1/v1/messages
        # 404s on OpenRouter — verified against the live API).
        "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
        "ANTHROPIC_AUTH_TOKEN": "sk-or-x",
        # Pinned EMPTY per OpenRouter's Claude Code cookbook, so an ambient
        # key can't fight the gateway credential.
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_MODEL": "anthropic/claude-4.5",
        # /model fetches the gateway's curated picker (a pinned model bypasses
        # it, per Claude Code's own precedence — asserted here to stay honest).
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    }
    assert args == ()


def test_openrouter_codex_openai_compatible():
    p = _profile(kind="openrouter", api_key="sk-or-x", model="qwen/qwen3-coder")
    env, args = ap.overlay_for("codex", p)
    assert env["OPENAI_BASE_URL"] == ap.OPENROUTER_BASE_URL
    assert env["OPENAI_API_BASE"] == ap.OPENROUTER_BASE_URL
    assert env["OPENAI_API_KEY"] == "sk-or-x"
    assert args == ("-m", "qwen/qwen3-coder")


def test_openrouter_aider_native_spelling():
    p = _profile(kind="openrouter", api_key="sk-or-x", model="deepseek/deepseek-v3")
    env, args = ap.overlay_for("aider", p)
    assert env == {"OPENROUTER_API_KEY": "sk-or-x"}
    assert args == ("--model", "openrouter/deepseek/deepseek-v3")


def test_openrouter_goose_env_only():
    p = _profile(kind="openrouter", api_key="sk-or-x", model="m")
    env, args = ap.overlay_for("goose", p)
    assert env == {
        "GOOSE_PROVIDER": "openrouter",
        "OPENROUTER_API_KEY": "sk-or-x",
        "GOOSE_MODEL": "m",
    }
    assert args == ()


def test_openrouter_custom_base_url_wins():
    p = _profile(kind="openrouter", api_key="k", base_url="https://or.example/v1")
    # The claude route strips a trailing /v1 (the SDK adds its own); the
    # OpenAI-compatible routes keep the base verbatim.
    env, _ = ap.overlay_for("claude", p)
    assert env["ANTHROPIC_BASE_URL"] == "https://or.example"
    env, _ = ap.overlay_for("codex", p)
    assert env["OPENAI_BASE_URL"] == "https://or.example/v1"


def test_openrouter_provider_scoped_restricts():
    p = _profile(kind="openrouter", provider="codex", api_key="k")
    assert ap.overlay_for("codex", p)[0]  # applies to its own CLI
    assert ap.overlay_for("aider", p) == ({}, ())  # not to others


def test_openrouter_unrouted_cli_is_a_no_op():
    p = _profile(kind="openrouter", api_key="k")
    assert ap.overlay_for("cline", p) == ({}, ())


def test_env_overrides_merge_last_and_apply_anywhere():
    p = _profile(
        kind="api_key",
        provider="claude",
        api_key="sk-1",
        env={"ANTHROPIC_API_KEY": "sk-2", "EXTRA": "1"},
    )
    env, _ = ap.overlay_for("claude", p)
    assert env["ANTHROPIC_API_KEY"] == "sk-2"  # raw env wins over the typed route
    assert env["EXTRA"] == "1"
    # env-only profiles reach CLIs with no typed route (user-defined providers)
    p2 = _profile(kind="account", provider="claude", env={"FOO": "bar"})
    assert ap.overlay_for("my-custom-cli", p2) == ({"FOO": "bar"}, ())


# --------------------------------------------------------------------------- #
# The tri-state and the settings store
# --------------------------------------------------------------------------- #
def _save_profiles(*profiles, default=""):
    S.set_auth_profiles([dict(p) for p in profiles])
    if default:
        S.update_settings(auth_profiles={"default_profile": default})


def test_effective_profile_id_tri_state():
    _save_profiles({"id": "work", "kind": "account"}, default="work")
    assert ap.effective_profile_id("") == "work"  # inherit the global default
    assert ap.effective_profile_id("default") == ""  # explicitly none
    assert ap.effective_profile_id("other") == "other"  # pinned


def test_env_var_overrides_default_profile(monkeypatch):
    _save_profiles({"id": "work", "kind": "account"}, default="work")
    monkeypatch.setenv("MINDFLOCK_AUTH_PROFILE", "personal")
    assert ap.effective_profile_id("") == "personal"
    monkeypatch.setenv("MINDFLOCK_AUTH_PROFILE", "default")
    assert ap.effective_profile_id("") == ""


def test_launch_overlay_resolves_through_settings(tmp_path):
    _save_profiles(
        {"id": "work", "kind": "account", "provider": "claude", "config_dir": "/tmp/w"},
        default="work",
    )
    env, args = launch_script.profile_overlay("claude", "")
    assert env == {"CLAUDE_CONFIG_DIR": "/tmp/w"} and args == ()
    # An explicit "default" pin suppresses the global default.
    assert launch_script.profile_overlay("claude", "default") == ({}, ())


def test_set_auth_profiles_clears_dangling_default():
    _save_profiles({"id": "work", "kind": "account"}, default="work")
    S.set_auth_profiles([{"id": "other", "kind": "account"}])
    assert S.load_settings().auth_profiles.default_profile == ""


def test_settings_round_trip_preserves_secret_and_env():
    _save_profiles(
        {
            "id": "or",
            "kind": "openrouter",
            "api_key": "sk-or-x",
            "model": "m",
            "env": {"A": "1"},
        }
    )
    p = S.load_settings().auth_profiles.profiles[0]
    assert (p.id, p.kind, p.api_key, p.model, p.env) == (
        "or",
        "openrouter",
        "sk-or-x",
        "m",
        {"A": "1"},
    )


def test_unknown_kind_degrades_to_account():
    _save_profiles({"id": "x", "kind": "wat"})
    assert S.load_settings().auth_profiles.profiles[0].kind == "account"


def test_claude_account_root_map(tmp_path):
    _save_profiles(
        {"id": "work", "kind": "account", "provider": "claude"},
        {"id": "codex-w", "kind": "account", "provider": "codex"},
        {"id": "or", "kind": "openrouter", "api_key": "k"},
    )
    roots = ap.claude_account_root_map()
    assert roots == {str(tmp_path / ".mindflock" / "accounts" / "work"): "work"}


def test_unsupported_note_names_the_mismatch():
    _save_profiles({"id": "or", "kind": "openrouter", "api_key": "k"})
    assert "no route" in ap.unsupported_note("cline", "or")
    assert ap.unsupported_note("claude", "or") == ""
    assert ap.unsupported_note("claude", "") == ""  # no profile in play


# --------------------------------------------------------------------------- #
# Launch integration (the standalone path; the launcher bytes are pinned by
# test_launch_parity.py)
# --------------------------------------------------------------------------- #
def test_launch_command_applies_default_profile():
    _save_profiles(
        {"id": "or", "kind": "openrouter", "api_key": "sk-or-x", "model": "m"},
        default="or",
    )
    preamble, cmd = launch_script.launch_command("codex")
    assert "export OPENAI_API_KEY=sk-or-x\n" in preamble
    assert "-m m" in cmd


def test_launch_command_without_profiles_is_unchanged():
    with_none = launch_script.launch_command("codex")
    _save_profiles({"id": "or", "kind": "openrouter", "api_key": "k"})  # no default
    assert launch_script.launch_command("codex") == with_none


# --------------------------------------------------------------------------- #
# Session field: emit-on-deviation persistence
# --------------------------------------------------------------------------- #
def test_instance_data_profile_id_absent_when_unset():
    assert "profile_id" not in InstanceData(title="t").to_dict()


def test_instance_data_profile_id_round_trips():
    d = InstanceData(title="t", profile_id="work").to_dict()
    assert d["profile_id"] == "work"
    assert InstanceData.from_dict(d).profile_id == "work"
    assert InstanceData.from_dict({"title": "t"}).profile_id == ""


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    S.invalidate()
    providers.rebuild_registry()
    from backend.web.server import app

    with TestClient(app) as c:
        yield c
    S.invalidate()
    providers.rebuild_registry()


class TestAuthProfilesApi:
    def test_put_and_get_masks_the_key(self, client):
        r = client.put(
            "/api/settings/auth-profiles",
            json={
                "profiles": [
                    {"id": "or", "kind": "openrouter", "api_key": "sk-or-secret"}
                ]
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["profiles"][0]["api_key"] == "•••set"
        assert "sk-or-secret" not in r.text
        assert S.load_settings().auth_profiles.profiles[0].api_key == "sk-or-secret"

    def test_masked_key_keeps_existing_on_resave(self, client):
        client.put(
            "/api/settings/auth-profiles",
            json={"profiles": [{"id": "or", "kind": "openrouter", "api_key": "sk-1"}]},
        )
        client.put(
            "/api/settings/auth-profiles",
            json={
                "profiles": [
                    {
                        "id": "or",
                        "kind": "openrouter",
                        "api_key": "•••set",
                        "model": "m",
                    }
                ]
            },
        )
        p = S.load_settings().auth_profiles.profiles[0]
        assert p.api_key == "sk-1" and p.model == "m"

    def test_settings_view_masks_profile_keys(self, client):
        client.put(
            "/api/settings/auth-profiles",
            json={"profiles": [{"id": "or", "kind": "openrouter", "api_key": "sk-2"}]},
        )
        s = client.get("/api/settings").json()["settings"]
        assert s["auth_profiles"]["profiles"][0]["api_key"] == "•••set"
        assert "sk-2" not in json.dumps(s)

    def test_default_profile_via_put_body(self, client):
        r = client.put(
            "/api/settings/auth-profiles",
            json={
                "profiles": [{"id": "work", "kind": "account"}],
                "default_profile": "work",
            },
        )
        assert r.json()["default_profile"] == "work"

    def test_default_profile_must_exist(self, client):
        r = client.post(
            "/api/settings", json={"auth_profiles": {"default_profile": "ghost"}}
        )
        assert r.status_code == 400
        assert "ghost" in r.json()["error"]

    def test_bad_id_and_kind_are_rejected(self, client):
        r = client.put(
            "/api/settings/auth-profiles",
            json={"profiles": [{"id": "Bad Id!", "kind": "account"}]},
        )
        assert r.status_code == 400
        r = client.put(
            "/api/settings/auth-profiles",
            json={"profiles": [{"id": "x", "kind": "wat"}]},
        )
        assert r.status_code == 400
        r = client.put(
            "/api/settings/auth-profiles",
            json={
                "profiles": [
                    {"id": "x", "kind": "account"},
                    {"id": "x", "kind": "account"},
                ]
            },
        )
        assert r.status_code == 400

    def test_account_profile_gets_its_dir_created(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        client.put(
            "/api/settings/auth-profiles",
            json={
                "profiles": [{"id": "work", "kind": "account", "provider": "claude"}]
            },
        )
        assert (tmp_path / ".mindflock" / "accounts" / "work").is_dir()

    def test_create_instance_rejects_unknown_profile(self, client):
        r = client.post("/api/instances", json={"title": "x", "profile_id": "ghost"})
        assert r.status_code == 400
        assert "ghost" in r.json()["error"]

    def test_swap_endpoint_validates(self, client):
        r = client.post("/api/instances/nope/profile", json={"profile_id": "whatever"})
        assert r.status_code == 404

    def test_openrouter_probe_without_key(self, client):
        r = client.post("/api/settings/test/openrouter", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False and "key" in body["error"]


# --------------------------------------------------------------------------- #
# probe_openrouter (urllib faked)
# --------------------------------------------------------------------------- #
def _fake_urlopen(responses):
    def opener(req, timeout=None):  # noqa: ARG001
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for suffix, payload in responses.items():
            if url.endswith(suffix):
                if isinstance(payload, Exception):
                    raise payload
                body = json.dumps(payload).encode()
                buf = io.BytesIO(body)
                buf.read = buf.read  # file-like
                return _FakeResp(body)
        raise AssertionError("unexpected url %s" % url)

    return opener


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_probe_openrouter_reports_usage_and_models(monkeypatch):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _fake_urlopen(
            {
                "/key": {"data": {"label": "sk-or…", "usage": 1.25, "limit": 10}},
                "/models": {"data": [{"id": "anthropic/claude-4.5"}, {"id": "x/y"}]},
            }
        ),
    )
    out = ap.probe_openrouter("sk-or-x")
    assert out["ok"] is True
    assert out["usage"] == 1.25 and out["limit"] == 10
    assert out["models"] == ["anthropic/claude-4.5", "x/y"]


def test_probe_openrouter_bad_key(monkeypatch):
    err = urllib.error.HTTPError("u", 401, "unauthorized", None, io.BytesIO(b""))
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen({"/key": err}))
    out = ap.probe_openrouter("sk-bad")
    assert out["ok"] is False and "invalid" in out["error"]


def test_probe_openrouter_no_key():
    out = ap.probe_openrouter("")
    assert out["ok"] is False and out["error"]


# --------------------------------------------------------------------------- #
# Per-session model override + agent routing (the New dialog's Model picker)
# --------------------------------------------------------------------------- #
def test_launch_overlay_model_override_beats_profile_pin():
    _save_profiles(
        {
            "id": "or",
            "kind": "openrouter",
            "api_key": "sk-or-x",
            "model": "anthropic/claude-4.5",
        }
    )
    env, _ = ap.launch_overlay("claude", "or", "qwen/qwen3-coder")
    assert env["ANTHROPIC_MODEL"] == "qwen/qwen3-coder"
    # Blank override keeps the profile's own pin.
    env, _ = ap.launch_overlay("claude", "or", "")
    assert env["ANTHROPIC_MODEL"] == "anthropic/claude-4.5"
    # The override reaches flag-shaped routes too.
    _, args = ap.launch_overlay("codex", "or", "deepseek/deepseek-v3")
    assert args == ("-m", "deepseek/deepseek-v3")


def test_supported_agents_matrix():
    assert ap.supported_agents(_profile(kind="openrouter", api_key="k")) == [
        "aider",
        "claude",
        "codex",
        "goose",
    ]
    assert ap.supported_agents(
        _profile(kind="openrouter", provider="codex", api_key="k")
    ) == ["codex"]
    assert ap.supported_agents(_profile(kind="account", provider="claude")) == [
        "claude"
    ]
    assert ap.supported_agents(_profile(kind="account", provider="codex")) == ["codex"]
    # A keyless key-profile routes nowhere (it would inject nothing).
    assert ap.supported_agents(_profile(kind="openrouter")) == []


def test_instance_data_profile_model_round_trips():
    assert "profile_model" not in InstanceData(title="t").to_dict()
    d = InstanceData(title="t", profile_id="or", profile_model="x/y").to_dict()
    assert d["profile_model"] == "x/y"
    assert InstanceData.from_dict(d).profile_model == "x/y"


class TestProfileModelApi:
    def test_view_includes_supported_agents(self, client):
        client.put(
            "/api/settings/auth-profiles",
            json={"profiles": [{"id": "or", "kind": "openrouter", "api_key": "k"}]},
        )
        view = client.get("/api/settings/auth-profiles").json()
        assert view["profiles"][0]["supported_agents"] == [
            "aider",
            "claude",
            "codex",
            "goose",
        ]

    def test_create_instance_rejects_multiline_model(self, client):
        client.put(
            "/api/settings/auth-profiles",
            json={"profiles": [{"id": "or", "kind": "openrouter", "api_key": "k"}]},
        )
        r = client.post(
            "/api/instances",
            json={"title": "x", "profile_id": "or", "profile_model": "a\nb"},
        )
        assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Review fixes: atomic PUT, rename-loses-key, per-account backfill
# --------------------------------------------------------------------------- #
class TestPutAtomicity:
    def test_bad_default_leaves_the_list_untouched(self, client):
        client.put(
            "/api/settings/auth-profiles",
            json={
                "profiles": [{"id": "personal", "kind": "openrouter", "api_key": "k"}]
            },
        )
        r = client.put(
            "/api/settings/auth-profiles",
            json={
                "profiles": [{"id": "work", "kind": "account"}],
                "default_profile": "personal",  # not in the NEW list
            },
        )
        assert r.status_code == 400
        # 400 must mean "nothing changed": the old profile (and its key) live.
        s = S.load_settings().auth_profiles
        assert [p.id for p in s.profiles] == ["personal"]
        assert s.profiles[0].api_key == "k"

    def test_clearing_the_default(self, client):
        client.put(
            "/api/settings/auth-profiles",
            json={
                "profiles": [{"id": "work", "kind": "account"}],
                "default_profile": "work",
            },
        )
        r = client.put(
            "/api/settings/auth-profiles",
            json={
                "profiles": [{"id": "work", "kind": "account"}],
                "default_profile": "",
            },
        )
        assert r.json()["default_profile"] == ""

    def test_rename_with_masked_key_is_rejected(self, client):
        client.put(
            "/api/settings/auth-profiles",
            json={"profiles": [{"id": "or", "kind": "openrouter", "api_key": "sk-1"}]},
        )
        r = client.put(
            "/api/settings/auth-profiles",
            json={
                "profiles": [
                    {"id": "openrouter", "kind": "openrouter", "api_key": "•••set"}
                ]
            },
        )
        assert r.status_code == 400
        assert "re-entering" in r.json()["error"]
        # And nothing changed: the original id still holds its key.
        s = S.load_settings().auth_profiles
        assert [p.id for p in s.profiles] == ["or"]
        assert s.profiles[0].api_key == "sk-1"


def test_backfill_uses_each_accounts_own_earliest_day(tmp_path, monkeypatch):
    """An account whose transcripts were pruned keeps its ledger days even when
    another account's scan reaches further back (the global-earliest gate would
    have dropped them from the per-account rows)."""
    import json as _json
    import time as _time

    from backend.providers import usage_history as uh

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MINDFLOCK_ASSISTANT_DIR", str(tmp_path / "assistant"))
    _save_profiles({"id": "work", "kind": "account", "provider": "claude"})
    # Ambient login has a transcript 20 days ago (global earliest day).
    now = _time.time()
    old_ts = now - 20 * 86400
    proj = tmp_path / ".claude" / "projects" / "p"
    proj.mkdir(parents=True)
    entry = {
        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S+00:00", _time.gmtime(old_ts)),
        "message": {"usage": {"input_tokens": 5, "output_tokens": 0}, "model": "m"},
    }
    (proj / "t.jsonl").write_text(_json.dumps(entry) + "\n")
    # The work account's transcripts are gone, but its ledger remembers a day
    # NEWER than the ambient scan's earliest — the case the fix is for.
    recent_day = _time.strftime("%Y-%m-%d", _time.localtime(now - 5 * 86400))
    ledger = {
        "days": {},
        "accounts": {
            "work": {
                recent_day: {
                    "in": 7,
                    "out": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "cost": 0.7,
                }
            }
        },
    }
    (tmp_path / "assistant").mkdir(exist_ok=True)
    (tmp_path / "assistant" / "usage-history.json").write_text(_json.dumps(ledger))
    acc, _recent, accounts = uh._compute()
    assert accounts["work"]["month"]["in"] == 7  # kept, not gated away


# --------------------------------------------------------------------------- #
# Second review round: reserved id, stale model on swap, env-value masking,
# and the launcher-rewrite settings fidelity
# --------------------------------------------------------------------------- #
class TestReservedId:
    def test_put_rejects_the_ambient_sentinel_as_an_id(self, client):
        r = client.put(
            "/api/settings/auth-profiles",
            json={"profiles": [{"id": "default", "kind": "account"}]},
        )
        assert r.status_code == 400
        assert "reserved" in r.json()["error"]

    def test_cli_add_rejects_it_too(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("MINDFLOCK_HOST", "127.0.0.1")
        monkeypatch.setenv("MINDFLOCK_PORT", "1")  # no server -> local path
        from backend.cli import main

        assert main(["accounts", "add", "default"]) == 1
        assert "reserved" in capsys.readouterr().err


class TestSwapModelPin:
    @pytest.fixture()
    def fake_inst(self, client, tmp_path, monkeypatch):
        """A minimal unstarted instance parked in ENGINE — enough for the swap
        endpoint's field logic (Started() is False, so no tmux is touched)."""
        monkeypatch.setenv("HOME", str(tmp_path))  # ENGINE.save -> tmp state
        from backend import session as _session
        from backend.web import server as _server

        inst = _session.Instance()
        inst.Title = "swap-me"
        inst.Program = "claude"
        with _server.ENGINE.lock:
            _server.ENGINE.instances["swap-me"] = inst
        yield inst
        with _server.ENGINE.lock:
            _server.ENGINE.instances.pop("swap-me", None)

    def test_identity_swap_drops_the_old_models_pin(self, client, fake_inst):
        client.put(
            "/api/settings/auth-profiles",
            json={
                "profiles": [
                    {"id": "or", "kind": "openrouter", "api_key": "k"},
                    {"id": "work", "kind": "account"},
                ]
            },
        )
        fake_inst.ProfileId = "or"
        fake_inst.ProfileModel = "openai/gpt-5"
        r = client.post("/api/instances/swap-me/profile", json={"profile_id": "work"})
        assert r.status_code == 200
        # The pin belonged to the OpenRouter catalog; carrying it onto a
        # Claude-subscription account would launch a nonexistent model.
        assert fake_inst.ProfileModel == ""

    def test_model_only_change_keeps_the_identity(self, client, fake_inst):
        client.put(
            "/api/settings/auth-profiles",
            json={"profiles": [{"id": "or", "kind": "openrouter", "api_key": "k"}]},
        )
        fake_inst.ProfileId = "or"
        fake_inst.ProfileModel = "openai/gpt-5"
        r = client.post(
            "/api/instances/swap-me/profile",
            json={"profile_id": "or", "profile_model": "qwen/qwen3-coder"},
        )
        assert r.status_code == 200
        assert fake_inst.ProfileId == "or"
        assert fake_inst.ProfileModel == "qwen/qwen3-coder"


class TestEnvValueMasking:
    def test_env_values_masked_on_both_reads(self, client):
        client.put(
            "/api/settings/auth-profiles",
            json={
                "profiles": [
                    {
                        "id": "or",
                        "kind": "openrouter",
                        "api_key": "sk-or-secret",
                        "env": {"ANTHROPIC_AUTH_TOKEN": "sk-ant-also-secret"},
                    }
                ]
            },
        )
        for path in ("/api/settings/auth-profiles", "/api/settings"):
            r = client.get(path)
            assert "sk-ant-also-secret" not in r.text, path
        view = client.get("/api/settings/auth-profiles").json()
        assert view["profiles"][0]["env"] == {"ANTHROPIC_AUTH_TOKEN": "•••set"}

    def test_masked_env_round_trips_and_edits_apply(self, client):
        client.put(
            "/api/settings/auth-profiles",
            json={
                "profiles": [
                    {
                        "id": "or",
                        "kind": "openrouter",
                        "api_key": "k",
                        "env": {"A": "secret-a", "B": "secret-b"},
                    }
                ]
            },
        )
        # Re-save what the UI received: A stays masked (kept), B gets a new
        # value, C is new.
        client.put(
            "/api/settings/auth-profiles",
            json={
                "profiles": [
                    {
                        "id": "or",
                        "kind": "openrouter",
                        "api_key": "•••set",
                        "env": {"A": "•••set", "B": "new-b", "C": "c"},
                    }
                ]
            },
        )
        p = S.load_settings().auth_profiles.profiles[0]
        assert p.env == {"A": "secret-a", "B": "new-b", "C": "c"}


def test_launcher_rewrite_preserves_skip_permissions_and_caches(tmp_path):
    """The swap-time rewrite must reuse the worktree's own provision settings —
    guessing once silently re-enabled --dangerously-skip-permissions for a
    session whose owner turned it off — and must refuse to rewrite without
    them."""
    from backend.session import provisioned as provisioning
    from backend.web import server as _server

    wt = tmp_path / "ws"
    wt.mkdir()
    launcher = wt / provisioning.LAUNCHER_BASENAME
    launcher.write_text("#!/usr/bin/env bash\noriginal\n")

    class _Wt:
        pass

    class _Inst:
        Provisioned = True
        Program = "claude"
        ProfileId = "or"
        ProfileModel = ""
        LaunchArgs = ()
        _git_worktree = _Wt()

    inst = _Inst()
    # No provision settings attached -> no rewrite, bytes untouched.
    inst._git_worktree._provision_settings = None
    assert _server._rewrite_launcher_for_profile(inst, str(wt)) is False
    assert launcher.read_text() == "#!/usr/bin/env bash\noriginal\n"
    # With settings: skip_permissions=False and a custom cache env survive.
    inst._git_worktree._provision_settings = provisioning.ProvisionSettings(
        repo_url=str(tmp_path),
        workspace_dir=tmp_path,
        base_branch="main",
        open_cursor=False,
        skip_permissions=False,
        setup_commands=[],
        caches=[],
    )
    assert _server._rewrite_launcher_for_profile(inst, str(wt)) is True
    script = launcher.read_text()
    assert "--dangerously-skip-permissions" not in script
