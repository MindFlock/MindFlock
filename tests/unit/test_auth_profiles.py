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
    assert ap.overlay_for("claude", p) == (
        {"ANTHROPIC_API_KEY": "sk-ant-x"},  # pragma: allowlist secret
        (),
    )  # pragma: allowlist secret


def test_api_key_codex_with_model_flag():
    p = _profile(kind="api_key", provider="codex", api_key="sk-x", model="gpt-5.2")
    env, args = ap.overlay_for("codex", p)
    assert env == {"OPENAI_API_KEY": "sk-x"}  # pragma: allowlist secret
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
    assert env["OPENAI_API_KEY"] == "sk-or-x"  # pragma: allowlist secret
    assert args == ("-m", "qwen/qwen3-coder")


def test_openrouter_aider_native_spelling():
    p = _profile(kind="openrouter", api_key="sk-or-x", model="deepseek/deepseek-v3")
    env, args = ap.overlay_for("aider", p)
    assert env == {"OPENROUTER_API_KEY": "sk-or-x"}  # pragma: allowlist secret
    assert args == ("--model", "openrouter/deepseek/deepseek-v3")


def test_openrouter_goose_env_only():
    p = _profile(kind="openrouter", api_key="sk-or-x", model="m")
    env, args = ap.overlay_for("goose", p)
    assert env == {
        "GOOSE_PROVIDER": "openrouter",
        "OPENROUTER_API_KEY": "sk-or-x",  # pragma: allowlist secret
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
        api_key="sk-1",  # pragma: allowlist secret
        env={"ANTHROPIC_API_KEY": "sk-2", "EXTRA": "1"},  # pragma: allowlist secret
    )
    env, _ = ap.overlay_for("claude", p)
    assert (
        env["ANTHROPIC_API_KEY"] == "sk-2"  # pragma: allowlist secret
    )  # raw env wins over the typed route  # pragma: allowlist secret
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
    assert env == {
        "CLAUDE_CONFIG_DIR": "/tmp/w",
        # Names the identity to the session itself, so its activity hook can
        # file the conversation id per account (thread_markers).
        ap.PROFILE_ID_ENV: "work",
    }
    assert args == ()
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
            "api_key": "sk-or-x",  # pragma: allowlist secret
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


def test_unknown_kind_is_kept_and_degrades_to_an_env_only_overlay():
    """An unrecognised kind must not be guessed into a typed one.

    Coercing it to "account" was the opposite of safe: that kind injects
    CLAUDE_CONFIG_DIR and would launch the CLI against an empty, logged-out
    config dir. Keeping the string means no typed overlay matches, so only the
    profile's raw ``env`` applies — which is what "unknown" should mean.
    """
    _save_profiles({"id": "x", "kind": "wat", "env": {"FOO": "1"}})
    prof = S.load_settings().auth_profiles.profiles[0]
    assert prof.kind == "wat"
    env, args = ap.overlay_for("claude", ap.get_profile("x"))
    assert env == {"FOO": "1"} and args == ()
    assert "CLAUDE_CONFIG_DIR" not in env


def test_a_kindless_profile_with_no_env_is_an_exact_no_op():
    _save_profiles({"id": "x", "kind": "wat"})
    assert ap.overlay_for("claude", ap.get_profile("x")) == ({}, ())


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
        {
            "id": "or",
            "kind": "openrouter",
            "api_key": "sk-or-x",  # pragma: allowlist secret
            "model": "m",
        },  # pragma: allowlist secret
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
                    {
                        "id": "or",
                        "kind": "openrouter",
                        "api_key": "sk-or-secret",  # pragma: allowlist secret
                    }  # pragma: allowlist secret
                ]
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["profiles"][0]["api_key"] == "•••set"
        assert "sk-or-secret" not in r.text
        assert (
            S.load_settings().auth_profiles.profiles[0].api_key
            == "sk-or-secret"  # pragma: allowlist secret
        )  # pragma: allowlist secret

    def test_masked_key_keeps_existing_on_resave(self, client):
        client.put(
            "/api/settings/auth-profiles",
            json={
                "profiles": [
                    {
                        "id": "or",
                        "kind": "openrouter",
                        "api_key": "sk-1",  # pragma: allowlist secret
                    }  # pragma: allowlist secret
                ]  # pragma: allowlist secret
            },  # pragma: allowlist secret
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
        assert p.api_key == "sk-1" and p.model == "m"  # pragma: allowlist secret

    def test_settings_view_masks_profile_keys(self, client):
        client.put(
            "/api/settings/auth-profiles",
            json={
                "profiles": [
                    {
                        "id": "or",
                        "kind": "openrouter",
                        "api_key": "sk-2",  # pragma: allowlist secret
                    }  # pragma: allowlist secret
                ]  # pragma: allowlist secret
            },  # pragma: allowlist secret
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

    def test_swap_endpoint_404s_for_an_unknown_session(self, client):
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
            "api_key": "sk-or-x",  # pragma: allowlist secret
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
            json={
                "profiles": [
                    {
                        "id": "or",
                        "kind": "openrouter",
                        "api_key": "sk-1",  # pragma: allowlist secret
                    }  # pragma: allowlist secret
                ]  # pragma: allowlist secret
            },  # pragma: allowlist secret
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
        assert s.profiles[0].api_key == "sk-1"  # pragma: allowlist secret


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
                        "api_key": "sk-or-secret",  # pragma: allowlist secret
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


# --------------------------------------------------------------------------- #
# The live hot-swap (POST /api/instances/{title}/profile)
#
# The headline of the feature and the one path that touches a RUNNING agent:
# it kills the tmux session and relaunches it under a different identity. Every
# test here asserts on what actually happened to the agent, not just on the
# status code.
# --------------------------------------------------------------------------- #
class _SwapInst:
    """The slice of an Instance the swap route reads."""

    def __init__(self, title, *, running=True, wt="/tmp/wt", program="claude"):
        from backend.session.storage import Status

        self.Title = title
        self.Program = program
        self.ProfileId = ""
        self.ProfileModel = ""
        self.Status = Status.Running if running else Status.Paused
        self.Provisioned = False
        self._wt = wt
        self._started = True

    def Started(self):  # noqa: N802
        return self._started

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


@pytest.fixture()
def swapping(client, monkeypatch):
    """A registered session plus a record of every agent kill/relaunch."""
    from backend.web import server

    calls: list = []
    inst = _SwapInst("swap-me")
    monkeypatch.setitem(server.ENGINE.instances, "swap-me", inst)
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: None)
    monkeypatch.setattr(
        server, "_kill_agent_session", lambda t: calls.append(("kill", t))
    )
    monkeypatch.setattr(
        server,
        "_ensure_agent_session",
        lambda i, t: (calls.append(("ensure", t)), ("agent-" + t, None))[1],
    )
    client.put(
        "/api/settings/auth-profiles",
        json={
            "profiles": [
                {"id": "work", "kind": "account", "provider": "claude"},
                {"id": "personal", "kind": "account", "provider": "claude"},
            ]
        },
    )
    yield client, inst, calls
    server.ENGINE.instances.pop("swap-me", None)


def test_swap_rejects_an_unknown_account_without_touching_the_agent(swapping):
    """A 400 from this route must mean the session is exactly as you left it."""
    client, inst, calls = swapping
    inst.ProfileId = "work"
    r = client.post("/api/instances/swap-me/profile", json={"profile_id": "ghost"})
    assert r.status_code == 400
    assert "ghost" in r.json()["error"]
    assert inst.ProfileId == "work"  # not mutated
    assert calls == []  # and the agent was never killed


def test_swap_rejects_a_malformed_model_before_mutating_anything(swapping):
    client, inst, calls = swapping
    inst.ProfileId, inst.ProfileModel = "work", "keep/me"
    r = client.post(
        "/api/instances/swap-me/profile",
        json={"profile_id": "personal", "profile_model": "a\nb"},
    )
    assert r.status_code == 400
    assert (inst.ProfileId, inst.ProfileModel) == ("work", "keep/me")
    assert calls == []


def test_swap_persists_the_pin_and_restarts_the_agent(swapping):
    client, inst, calls = swapping
    r = client.post("/api/instances/swap-me/profile", json={"profile_id": "work"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert inst.ProfileId == "work"
    # Killed, then relaunched — that pair IS the swap.
    assert calls == [("kill", "swap-me"), ("ensure", "swap-me")]


def test_swap_to_the_same_account_is_a_no_op_not_a_restart(swapping):
    """Re-picking the active row must not spend a working agent's resume
    position to arrive back where it started."""
    client, inst, calls = swapping
    client.post("/api/instances/swap-me/profile", json={"profile_id": "work"})
    calls.clear()
    r = client.post("/api/instances/swap-me/profile", json={"profile_id": "work"})
    assert r.status_code == 200
    assert r.json()["unchanged"] is True
    assert calls == []


def test_changing_identity_drops_a_model_pin_from_the_old_catalog(swapping):
    client, inst, _ = swapping
    client.post(
        "/api/instances/swap-me/profile",
        json={"profile_id": "work", "profile_model": "openai/gpt-5"},
    )
    assert inst.ProfileModel == "openai/gpt-5"
    client.post("/api/instances/swap-me/profile", json={"profile_id": "personal"})
    assert inst.ProfileModel == ""  # would not exist on the new account


def test_a_model_only_change_keeps_the_identity(swapping):
    client, inst, calls = swapping
    client.post("/api/instances/swap-me/profile", json={"profile_id": "work"})
    calls.clear()
    r = client.post(
        "/api/instances/swap-me/profile",
        json={"profile_id": "work", "profile_model": "anthropic/claude-sonnet-4.5"},
    )
    assert r.status_code == 200
    assert (inst.ProfileId, inst.ProfileModel) == (
        "work",
        "anthropic/claude-sonnet-4.5",
    )
    assert calls == [("kill", "swap-me"), ("ensure", "swap-me")]


def test_a_relaunch_failure_is_reported_instead_of_ok(swapping, monkeypatch):
    """The kill already happened, so a failed relaunch leaves no agent at all —
    answering ok:true would put a cheerful toast on top of a dead pane."""
    from backend.web import server

    client, inst, _ = swapping
    monkeypatch.setattr(
        server, "_ensure_agent_session", lambda i, t: ("", "workspace is gone")
    )
    r = client.post("/api/instances/swap-me/profile", json={"profile_id": "work"})
    assert r.status_code == 500
    assert "workspace is gone" in r.json()["error"]
    assert inst.ProfileId == "work"  # the pin still landed


def test_a_paused_session_takes_the_pin_without_a_restart(swapping):
    """Nothing is running to restart; the next start picks the pin up."""
    from backend.session.storage import Status

    client, inst, calls = swapping
    inst.Status = Status.Paused
    r = client.post("/api/instances/swap-me/profile", json={"profile_id": "work"})
    assert r.status_code == 200
    assert inst.ProfileId == "work"
    assert calls == []


def test_swap_emits_session_profile_changed(swapping):
    from backend.web.core import events as _events

    client, _inst, _calls = swapping
    seen: list = []
    unsub = _events.BUS.subscribe(lambda env: seen.append(env))
    try:
        client.post("/api/instances/swap-me/profile", json={"profile_id": "work"})
    finally:
        unsub()
    changed = [e for e in seen if e["event"] == "session.profile_changed"]
    assert changed, "the swap published nothing"
    assert changed[-1]["session"] == "swap-me"
    assert changed[-1]["data"]["profile_id"] == "work"
    # And the name is part of the published vocabulary, so user hooks can key
    # a ~/.mindflock/hooks/<event>/ directory off it.
    assert "session.profile_changed" in _events.EVENT_NAMES


# --------------------------------------------------------------------------- #
# Precedence against the local-model overlay
#
# Both features route a CLI, and they route it to opposite places. The env
# halves have always resolved local-wins (callers merge ``{**prof, **local}``),
# but the FLAG halves only concatenate — so a profile's ``-m`` landing after
# the local overlay's would win on every CLI that takes the last flag and
# quietly pull an on-machine session out to a gateway.
# --------------------------------------------------------------------------- #
def _use_local_model(monkeypatch, **kw):
    from backend.providers import local_models as lm

    cfg = lm.LocalModelConfig(
        enabled=True, runtime="ollama", base_url="", model="qwen2.5-coder:7b", **kw
    )
    monkeypatch.setattr(lm, "load_config", lambda: cfg)
    return cfg


def _key_profile(monkeypatch, provider="codex", model="openai/gpt-5"):
    prof = ap.AuthProfileConfig(
        id="k",
        kind="api_key",
        provider=provider,
        api_key="sk-x",  # pragma: allowlist secret
        model=model,  # pragma: allowlist secret
    )
    monkeypatch.setattr(ap, "get_profile", lambda pid: prof)
    monkeypatch.setattr(ap, "effective_profile_id", lambda pid: "k")
    return prof


def test_profile_flags_alone_survive_when_local_models_are_off(monkeypatch):
    """The baseline: with no local model in play the profile routes freely."""
    _key_profile(monkeypatch)
    _env, args = launch_script.profile_overlay("codex")
    assert args == ("-m", "openai/gpt-5")


def test_local_models_outrank_a_profiles_model_flag(monkeypatch):
    """A session configured to stay on this machine cannot be pulled off it by
    an account pin — the profile's routing FLAGS drop out."""
    _use_local_model(monkeypatch)
    _key_profile(monkeypatch)
    _env, args = launch_script.profile_overlay("codex")
    assert args == ()
    # ...and the composed command therefore carries exactly one -m, the local
    # server's model, not the gateway's.
    _lenv, local_args = launch_script.local_overlay("codex")
    combined = tuple(local_args) + tuple(args)
    assert combined.count("-m") == 1
    assert "qwen2.5-coder:7b" in combined
    assert "openai/gpt-5" not in combined


def test_a_profiles_env_still_applies_under_a_local_model(monkeypatch):
    """Only the flags are dropped. Identity env (an account's config dir) is
    orthogonal to where the model is served, and the callers' ``{**prof,
    **local}`` merge already lets local win any key they share."""
    _use_local_model(monkeypatch)
    _key_profile(monkeypatch)
    env, _args = launch_script.profile_overlay("codex")
    assert env.get("OPENAI_API_KEY") == "sk-x"  # pragma: allowlist secret


def test_a_cli_with_no_local_route_keeps_its_profile_routing(monkeypatch):
    """Claude Code speaks only the Anthropic API, so the local overlay cannot
    touch it. Turning local models ON must not strip an account's routing from
    the one CLI the feature was never able to serve anyway."""
    _use_local_model(monkeypatch)
    _key_profile(monkeypatch, provider="claude", model="")
    assert launch_script.local_overlay("claude") == ({}, ())
    env, _args = launch_script.profile_overlay("claude")
    assert env.get("ANTHROPIC_API_KEY") == "sk-x"  # pragma: allowlist secret


# --------------------------------------------------------------------------- #
# Pause -> resume must not launder the identity away
#
# ExtraEnv is deliberately not persisted (it is re-derived from settings on
# every start), and the resume route REPLACES the dict with a fresh port block.
# Anything else the session needed in its tmux env has to be rebuilt there too,
# or a paused profiled session comes back on the CLI's ambient login.
# --------------------------------------------------------------------------- #
class _PausedInst:
    """Enough of an Instance for the resume route and the snapshot it returns."""

    InPlace = False
    Provisioned = False
    LaunchArgs: tuple = ()

    def __init__(self, title):
        from backend.session.storage import Status

        self.Title = title
        self.Branch = "feat/x"
        self.Path = ""
        self.Program = "claude"
        self.ProfileId = "work"
        self.ProfileModel = ""
        self.Status = Status.Paused
        self.ExtraEnv: dict = {}
        self._started = True

    def Started(self):  # noqa: N802
        return self._started

    def GetWorktreePath(self):  # noqa: N802
        return ""

    def Resume(self):  # noqa: N802
        from backend.session.storage import Status

        self.Status = Status.Running


def test_resume_rebuilds_the_profile_env_instead_of_clobbering_it(
    client, monkeypatch, tmp_path
):
    from backend.web import server

    client.put(
        "/api/settings/auth-profiles",
        json={"profiles": [{"id": "work", "kind": "account", "provider": "claude"}]},
    )
    inst = _PausedInst("paused-one")
    monkeypatch.setitem(server.ENGINE.instances, "paused-one", inst)
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: None)
    monkeypatch.setattr(server._ports, "env_for", lambda t: {"PORT": "43110"})
    try:
        r = client.post("/api/instances/paused-one/resume")
        assert r.status_code == 200
        # The port block still lands...
        assert inst.ExtraEnv["PORT"] == "43110"
        # ...and so does the identity, or this session just resumed as someone
        # else.
        assert "CLAUDE_CONFIG_DIR" in inst.ExtraEnv
        assert inst.ExtraEnv["CLAUDE_CONFIG_DIR"].endswith("accounts/work")
    finally:
        server.ENGINE.instances.pop("paused-one", None)


def test_resume_of_an_unprofiled_session_carries_only_the_port_block(
    client, monkeypatch
):
    """No profile, no local model: byte-identical to the pre-feature resume."""
    from backend.web import server

    inst = _PausedInst("paused-plain")
    inst.ProfileId = "default"  # explicitly the CLI's own login
    monkeypatch.setitem(server.ENGINE.instances, "paused-plain", inst)
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: None)
    monkeypatch.setattr(server._ports, "env_for", lambda t: {"PORT": "43110"})
    try:
        assert client.post("/api/instances/paused-plain/resume").status_code == 200
        assert inst.ExtraEnv == {"PORT": "43110"}
    finally:
        server.ENGINE.instances.pop("paused-plain", None)


# --------------------------------------------------------------------------- #
# Removing an account a session is running as
# --------------------------------------------------------------------------- #
def _pinned_session(monkeypatch, title, profile_id):
    from backend.web import server

    inst = _SwapInst(title)
    inst.ProfileId = profile_id
    monkeypatch.setitem(server.ENGINE.instances, title, inst)
    return inst


def test_removing_an_account_in_use_is_refused_and_names_the_sessions(
    client, monkeypatch
):
    from backend.web import server

    client.put(
        "/api/settings/auth-profiles",
        json={"profiles": [{"id": "work", "kind": "account", "provider": "claude"}]},
    )
    _pinned_session(monkeypatch, "on-work", "work")
    try:
        r = client.put("/api/settings/auth-profiles", json={"profiles": []})
        assert r.status_code == 409
        assert "on-work" in r.json()["error"]
        assert r.json()["in_use"] == ["on-work"]
        # ...and nothing was written.
        assert [
            p["id"]
            for p in client.get("/api/settings/auth-profiles").json()["profiles"]
        ] == ["work"]
    finally:
        server.ENGINE.instances.pop("on-work", None)


def test_force_removes_an_account_in_use(client, monkeypatch):
    from backend.web import server

    client.put(
        "/api/settings/auth-profiles",
        json={"profiles": [{"id": "work", "kind": "account", "provider": "claude"}]},
    )
    _pinned_session(monkeypatch, "on-work", "work")
    try:
        r = client.put(
            "/api/settings/auth-profiles", json={"profiles": [], "force": True}
        )
        assert r.status_code == 200
        assert r.json()["profiles"] == []
    finally:
        server.ENGINE.instances.pop("on-work", None)


def test_a_session_that_only_INHERITS_the_default_does_not_block_a_removal(
    client, monkeypatch
):
    """An unpinned session asked to follow the default; following it to a new
    value is the behaviour it signed up for."""
    from backend.web import server

    client.put(
        "/api/settings/auth-profiles",
        json={
            "profiles": [{"id": "work", "kind": "account", "provider": "claude"}],
            "default_profile": "work",
        },
    )
    _pinned_session(monkeypatch, "inheriting", "")
    try:
        r = client.put(
            "/api/settings/auth-profiles",
            json={"profiles": [], "default_profile": ""},
        )
        assert r.status_code == 200
    finally:
        server.ENGINE.instances.pop("inheriting", None)


def test_editing_an_account_in_place_is_not_a_removal(client, monkeypatch):
    """Only ids that DISAPPEAR count — relabelling one must not 409."""
    from backend.web import server

    client.put(
        "/api/settings/auth-profiles",
        json={"profiles": [{"id": "work", "kind": "account", "provider": "claude"}]},
    )
    _pinned_session(monkeypatch, "on-work", "work")
    try:
        r = client.put(
            "/api/settings/auth-profiles",
            json={
                "profiles": [
                    {
                        "id": "work",
                        "kind": "account",
                        "provider": "claude",
                        "label": "Work (renamed)",
                    }
                ]
            },
        )
        assert r.status_code == 200
    finally:
        server.ENGINE.instances.pop("on-work", None)


def test_an_unrouted_profile_does_not_tag_the_session_with_an_identity(tmp_path):
    """No route means the session runs on the ambient login, so it must look
    exactly like an unprofiled one — the hook must not file its conversation
    under an account that is not actually serving it."""
    _save_profiles(
        {"id": "or", "kind": "openrouter", "api_key": "k"},
        default="or",  # pragma: allowlist secret
    )
    assert launch_script.profile_overlay("cline", "") == ({}, ())


# --------------------------------------------------------------------------- #
# Per-account conversations
#
# A thread belongs to the account that created it — its transcript lives under
# that identity's config dir and the other one cannot open it. Before this,
# one marker per window meant a swap asked the new identity to resume a thread
# it had never seen AND overwrote the id that could have taken you back, so
# swapping back started a third fresh conversation.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def markers(tmp_path, monkeypatch):
    from backend.providers import thread_markers as tm

    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(tmp_path / "threads"))
    return tm


def test_an_unprofiled_record_writes_exactly_one_file(markers, tmp_path):
    """The pre-profiles layout, byte for byte."""
    markers.record("win", "tid-1")
    assert sorted(p.name for p in (tmp_path / "threads").iterdir()) == ["win.thread"]
    assert markers.read("win") == "tid-1"


def test_a_profiled_record_also_files_a_per_account_memory(markers, tmp_path):
    markers.record("win", "tid-work", "work")
    assert sorted(p.name for p in (tmp_path / "threads").iterdir()) == [
        "win.thread",
        "win@work.thread",
    ]
    assert markers.read("win") == "tid-work"
    assert markers.remembered("win", "work") == "tid-work"
    assert markers.remembered("win", "personal") == ""


def test_swapping_back_restores_the_conversation_you_left(markers):
    markers.record("win", "tid-work", "work")
    # -> personal: nothing remembered, so the relaunch must start fresh rather
    #    than ask personal to resume work's thread.
    assert markers.switch_profile("win", "work", "personal") == ""
    assert markers.read("win") == ""
    markers.record("win", "tid-personal", "personal")
    # -> back to work: its own thread returns.
    assert markers.switch_profile("win", "personal", "work") == "tid-work"
    assert markers.read("win") == "tid-work"
    # ...and personal's is still filed, so the next hop back also lands.
    assert markers.switch_profile("win", "work", "personal") == "tid-personal"


def test_swapping_files_the_outgoing_thread_even_if_it_moved_on(markers):
    """The current marker can advance after the last record (the hook writes it
    on every turn); the swap must file what is there NOW, not a stale copy."""
    markers.record("win", "tid-a", "work")
    markers.record("win", "tid-a2", "work")
    markers.switch_profile("win", "work", "personal")
    assert markers.remembered("win", "work") == "tid-a2"


def test_swapping_to_and_from_the_ambient_login_round_trips(markers):
    markers.record("win", "tid-ambient")  # no profile: current marker only
    assert markers.switch_profile("win", "", "work") == ""
    markers.record("win", "tid-work", "work")
    # Back to ambient: its thread was filed under "" -> the current marker,
    # which the swap away rewrote. The ambient identity keeps its own memory
    # file so the round trip still lands.
    assert markers.switch_profile("win", "work", "") == "tid-ambient"


def test_a_swap_to_the_same_identity_leaves_the_marker_alone(markers):
    markers.record("win", "tid-1", "work")
    assert markers.switch_profile("win", "work", "work") == "tid-1"
    assert markers.read("win") == "tid-1"


def test_claimed_never_counts_a_windows_own_memories_against_it(markers):
    """`claimed` stops a window binding to a SIBLING's conversation. A window's
    own per-account memories are not a sibling's — counting them would stop it
    re-binding to its own thread after a swap back."""
    markers.record("win", "tid-mine", "work")
    markers.record("other", "tid-theirs", "work")
    assert markers.claimed(exclude_session="win") == {"tid-theirs"}
    assert markers.claimed(exclude_session="other") == {"tid-mine"}


def test_a_fresh_start_clears_the_current_marker_but_keeps_the_memories(markers):
    markers.record("win", "tid-work", "work")
    markers.clear("win")
    assert markers.read("win") == ""
    assert markers.remembered("win", "work") == "tid-work"
    markers.clear("win", forget_accounts=True)
    assert markers.remembered("win", "work") == ""


def test_the_activity_hook_files_the_memory_from_inside_the_session(
    tmp_path, monkeypatch
):
    """The hook is generated shell, so the only honest test runs it. It learns
    the identity from the session env the overlay exports — the server is not
    in the loop."""
    import json as _json
    import os
    import subprocess

    from backend.providers import activity_markers as am

    td = tmp_path / "threads"
    # The hook bakes the marker dir in at GENERATION time, so this has to be
    # set before hook_command() runs — not merely handed to the subprocess.
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(td))
    cmd = am.hook_command("working", str(tmp_path / "activity"), record_thread=True)
    env = {
        **os.environ,
        "MINDFLOCK_SESSION_NAME": "win1",
        ap.PROFILE_ID_ENV: "work",
    }
    subprocess.run(
        ["bash", "-c", cmd],
        input=_json.dumps({"session_id": "tid-from-hook"}),
        text=True,
        env=env,
        check=True,
    )
    assert (td / "win1.thread").read_text() == "tid-from-hook"
    assert (td / "win1@work.thread").read_text() == "tid-from-hook"


def test_the_activity_hook_writes_only_the_plain_marker_without_a_profile(
    tmp_path, monkeypatch
):
    import json as _json
    import os
    import subprocess

    from backend.providers import activity_markers as am

    td = tmp_path / "threads"
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(td))
    cmd = am.hook_command("working", str(tmp_path / "activity"), record_thread=True)
    env = {**os.environ, "MINDFLOCK_SESSION_NAME": "win1"}
    env.pop(ap.PROFILE_ID_ENV, None)
    subprocess.run(
        ["bash", "-c", cmd],
        input=_json.dumps({"session_id": "tid-plain"}),
        text=True,
        env=env,
        check=True,
    )
    assert [p.name for p in td.iterdir()] == ["win1.thread"]


def test_the_swap_route_reports_whether_the_conversation_came_back(
    swapping, tmp_path, monkeypatch
):
    from backend.providers import thread_markers as tm
    from backend.session import tmux

    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(tmp_path / "threads"))
    client, _inst, _calls = swapping
    name = tmux.to_mindflock_tmux_name("swap-me")

    # First hop onto an account it has never run as: nothing to resume.
    r = client.post("/api/instances/swap-me/profile", json={"profile_id": "work"})
    assert r.json()["resumed"] is False
    tm.record(name, "tid-work", "work")

    r = client.post("/api/instances/swap-me/profile", json={"profile_id": "personal"})
    assert r.json()["resumed"] is False
    assert tm.read(name) == ""  # not asked to resume work's thread
    tm.record(name, "tid-personal", "personal")

    # ...and back.
    r = client.post("/api/instances/swap-me/profile", json={"profile_id": "work"})
    assert r.json()["resumed"] is True
    assert tm.read(name) == "tid-work"


# --------------------------------------------------------------------------- #
# The surfaces that were blind to a profile
# --------------------------------------------------------------------------- #
def test_pre_trust_seeds_every_claude_account_dir(tmp_path, monkeypatch):
    """Trust is a property of the FOLDER — MindFlock created the worktree — so
    it must be seeded in whichever config the session's identity will read, or
    a profiled run stalls at an invisible "do you trust this folder?" gate."""
    import json as _json

    from backend.providers import claude as _claude

    monkeypatch.delenv("MINDFLOCK_CLAUDE_JSON", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _save_profiles({"id": "work", "kind": "account", "provider": "claude"})
    wt = tmp_path / "wt"
    wt.mkdir()
    _claude.pre_trust_workdir(str(wt))

    acct = tmp_path / ".mindflock" / "accounts" / "work" / ".claude.json"
    assert acct.is_file(), "the account's config was never seeded"
    entry = _json.loads(acct.read_text())["projects"][str(wt.resolve())]
    assert entry["hasTrustDialogAccepted"] is True
    # ...and the ambient one still is too.
    home = tmp_path / ".claude.json"
    assert _json.loads(home.read_text())["projects"][str(wt.resolve())][
        "hasTrustDialogAccepted"
    ]


def test_pre_trust_touches_only_the_ambient_config_with_no_profiles(
    tmp_path, monkeypatch
):
    from backend.providers import claude as _claude

    monkeypatch.delenv("MINDFLOCK_CLAUDE_JSON", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    wt = tmp_path / "wt"
    wt.mkdir()
    _claude.pre_trust_workdir(str(wt))
    assert (tmp_path / ".claude.json").is_file()
    assert not (tmp_path / ".mindflock" / "accounts").exists()


@pytest.fixture()
def create(client, tmp_path, monkeypatch):
    """POST /api/instances without actually starting anything.

    The route answers 202 and hands the real work to a background task; letting
    that run would fork a pty and create tmux sessions on the developer's
    machine. Closing the coroutine keeps the response path honest and the side
    effects nil.
    """
    from backend.web import server

    def _close(coro):
        try:
            coro.close()
        except AttributeError:
            pass
        return None

    monkeypatch.setattr(server, "_register_task", _close)
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: None)
    made: list = []

    def _post(title, **kw):
        d = tmp_path / title
        d.mkdir(parents=True, exist_ok=True)
        made.append(title)
        return client.post(
            "/api/instances",
            json={"title": title, "repo_path": str(d), "in_place": True, **kw},
        )

    yield _post
    for t in made:
        server.ENGINE.instances.pop(t, None)


def test_creating_a_session_warns_when_the_account_cannot_route_the_agent(
    client, create
):
    """The New dialog warns at selection time; API and CLI callers heard
    nothing at all, and a session running as the wrong identity is the one
    outcome this feature cannot be quiet about."""
    client.put(
        "/api/settings/auth-profiles",
        json={
            "profiles": [
                {
                    "id": "or",
                    "kind": "openrouter",
                    "api_key": "sk-or-x",  # pragma: allowlist secret
                }  # pragma: allowlist secret
            ]
        },
    )
    r = create("no-route", program="cline", profile_id="or")
    assert r.status_code == 202, r.json()
    assert "no route for" in (r.json().get("note") or "")


def test_a_routable_combination_carries_no_warning(client, create):
    client.put(
        "/api/settings/auth-profiles",
        json={"profiles": [{"id": "work", "kind": "account", "provider": "claude"}]},
    )
    r = create("routable", program="claude", profile_id="work")
    assert r.status_code == 202, r.json()
    assert "note" not in r.json()


def test_an_unprofiled_session_carries_no_warning(create):
    r = create("plain", program="claude")
    assert r.status_code == 202, r.json()
    assert "note" not in r.json()


def test_the_env_pinned_default_is_reported_instead_of_the_stored_one(
    client, monkeypatch
):
    """$MINDFLOCK_AUTH_PROFILE wins at launch and is read from the server's own
    env. Reporting the stored value would have the screen name one identity
    while every session runs as another."""
    client.put(
        "/api/settings/auth-profiles",
        json={
            "profiles": [
                {"id": "work", "kind": "account", "provider": "claude"},
                {"id": "personal", "kind": "account", "provider": "claude"},
            ],
            "default_profile": "work",
        },
    )
    assert client.get("/api/settings/auth-profiles").json()["default_profile"] == "work"
    monkeypatch.setenv("MINDFLOCK_AUTH_PROFILE", "personal")
    body = client.get("/api/settings/auth-profiles").json()
    assert body["default_profile"] == "personal"
    assert body["default_profile_env"] == "personal"
    assert body["default_profile_locked"] is True


def test_the_assistant_runs_under_the_app_default_account(tmp_path, monkeypatch):
    """The Assistant is a session too: with a default account configured it
    should not quietly spend the CLI's ambient login."""
    import inspect

    from backend.web.addons import assistant

    src = inspect.getsource(assistant)
    assert "profile_overlay" in src, "the Assistant never applies the auth profile"
    _save_profiles(
        {"id": "work", "kind": "account", "provider": "claude", "config_dir": "/tmp/w"},
        default="work",
    )
    env, _args = launch_script.profile_overlay(assistant._assistant_program())
    assert env.get("CLAUDE_CONFIG_DIR") == "/tmp/w"


def test_the_plan_window_is_scoped_to_the_metered_identity(monkeypatch):
    """usage_live() reads the server's own credentials, so pricing that plan's
    window with every account's turns would anchor it on a message the plan
    never saw."""
    from backend.providers import usage_history
    from backend.web.core import usage_api

    claude = providers.resolve("claude")
    assert usage_api._window_account(claude) is None  # no profiles: unchanged
    _save_profiles({"id": "work", "kind": "account", "provider": "claude"})
    assert usage_api._window_account(claude) == usage_history.AMBIENT_ACCOUNT


def test_current_window_filters_by_account(monkeypatch):
    import time as _time

    from backend.providers import usage_history as uh

    now = _time.time()
    tok = {"input_tokens": 10, "output_tokens": 5}
    monkeypatch.setattr(uh, "_refresh", lambda: None)
    uh._cache["recent"] = [
        (now - 60, 1.0, tok, uh.AMBIENT_ACCOUNT),
        (now - 30, 2.0, tok, "work"),
    ]
    try:
        assert uh.current_window(5.0)["cost"] == pytest.approx(3.0)  # merged
        ambient = uh.current_window(5.0, uh.AMBIENT_ACCOUNT)
        assert ambient["cost"] == pytest.approx(1.0)
        assert uh.current_window(5.0, "work")["cost"] == pytest.approx(2.0)
        assert uh.current_window(5.0, "nobody") is None
    finally:
        uh._cache["recent"] = None
