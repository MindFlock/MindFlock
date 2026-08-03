"""Local-model routing: the overlays that keep a session on this machine.

The promise is absolute — with local models on, no prompt, diff or file leaves
the box — so these tests pin the two ways it could be silently broken:

  * an overlay that is WRONG for a CLI (bad flag/env name) makes the CLI either
    refuse to start or quietly fall back to its hosted API,
  * an overlay that is MISSING where it should apply means the session runs
    against the vendor API while the UI claims otherwise.

Every mapping asserted here was verified against an installed binary or the
CLI's own bundled docs — see ``backend/providers/local_models`` for the citation
per CLI. Pure-python: no servers, no tmux, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.providers import local_models as lm
from backend.providers.local_models import LocalModelConfig
from backend.session import provisioned


def _cfg(**kw) -> LocalModelConfig:
    base = dict(enabled=True, runtime="ollama", base_url="", model="qwen2.5-coder:7b")
    base.update(kw)
    return LocalModelConfig(**base)


# --------------------------------------------------------------------------- #
# Off / unconfigured / unsupported must all be exact no-ops.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "cfg",
    [
        LocalModelConfig(),  # disabled
        _cfg(enabled=False),
        _cfg(model=""),  # on but no model chosen
        _cfg(model="   "),
        _cfg(runtime="nonsense"),
    ],
)
def test_unconfigured_overlay_is_a_no_op(cfg):
    """ "Local models off" has to mean byte-for-byte the old behaviour: callers
    apply the overlay unconditionally."""
    assert lm.overlay_for("codex", cfg) == ({}, ())
    assert lm.overlay_for("aider", cfg) == ({}, ())
    assert lm.overlay_for("goose", cfg) == ({}, ())


def test_claude_is_reported_unsupported_not_silently_overlaid():
    """Claude Code speaks only the Anthropic API. Inventing env for it would
    either break the launch or leave it on the hosted API while the UI says
    local — so it must be reported as having no route."""
    assert lm.supported("claude") is False
    assert lm.overlay_for("claude", _cfg()) == ({}, ())


def test_unsupported_agent_gets_an_explicit_warning(monkeypatch):
    monkeypatch.setattr(lm, "load_config", lambda: _cfg())
    note = lm.unsupported_note("claude")
    assert "hosted API" in note
    # It must name the way out, not just the problem.
    assert "codex" in note and "aider" in note and "goose" in note
    assert lm.unsupported_note("codex") == ""


def test_no_note_when_the_feature_is_off(monkeypatch):
    monkeypatch.setattr(lm, "load_config", lambda: LocalModelConfig())
    assert lm.unsupported_note("claude") == ""


# --------------------------------------------------------------------------- #
# Per-CLI overlays (verified spellings).
# --------------------------------------------------------------------------- #
def test_codex_uses_its_native_oss_flags():
    # codex --help: `--oss` selects the open-source provider, `--local-provider`
    # picks which local server serves it.
    env, args = lm.overlay_for("codex", _cfg(runtime="ollama"))
    assert env == {}
    assert args == ("--oss", "--local-provider", "ollama", "-m", "qwen2.5-coder:7b")
    _, args = lm.overlay_for("codex", _cfg(runtime="lmstudio"))
    assert "--local-provider" in args and "lmstudio" in args


def test_codex_custom_runtime_falls_back_to_openai_compatible_env():
    """--local-provider only accepts ollama|lmstudio, so a llama.cpp/vLLM
    endpoint must NOT be passed as one — it goes through the OpenAI-compatible
    path instead of a value codex would reject."""
    env, args = lm.overlay_for(
        "codex", _cfg(runtime="custom", base_url="http://127.0.0.1:9000/v1")
    )
    assert "--local-provider" not in args
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:9000/v1"
    assert env["OPENAI_API_KEY"]  # non-empty: an empty Bearer is rejected


def test_aider_ollama_matches_its_own_docs():
    # aider/website/docs/llms/ollama.md: OLLAMA_API_BASE + the ollama_chat/ prefix
    # (the docs explicitly recommend ollama_chat/ over ollama/).
    env, args = lm.overlay_for("aider", _cfg(runtime="ollama"))
    assert env == {"OLLAMA_API_BASE": "http://127.0.0.1:11434"}
    assert args == ("--model", "ollama_chat/qwen2.5-coder:7b")


def test_aider_lmstudio_sets_the_required_dummy_key():
    # aider/website/docs/llms/lm-studio.md is explicit: LM_STUDIO_API_KEY must be
    # non-empty or the client fails sending an empty Bearer token.
    env, args = lm.overlay_for("aider", _cfg(runtime="lmstudio", model="qwen"))
    assert env["LM_STUDIO_API_BASE"] == "http://127.0.0.1:1234/v1"
    assert env["LM_STUDIO_API_KEY"]
    assert args == ("--model", "lm_studio/qwen")


def test_goose_is_env_only_and_has_no_model_flag():
    # goose takes GOOSE_PROVIDER/GOOSE_MODEL (verified in the binary's strings)
    # and has no --model flag, so args must stay empty.
    env, args = lm.overlay_for("goose", _cfg(runtime="ollama"))
    assert args == ()
    assert env["GOOSE_PROVIDER"] == "ollama"
    assert env["GOOSE_MODEL"] == "qwen2.5-coder:7b"
    assert env["OLLAMA_HOST"] == "http://127.0.0.1:11434"


def test_explicit_base_url_overrides_the_runtime_default():
    env, _ = lm.overlay_for(
        "aider", _cfg(runtime="ollama", base_url="http://gpu-box.local:11434")
    )
    assert env == {"OLLAMA_API_BASE": "http://gpu-box.local:11434"}


@pytest.mark.parametrize("runtime", lm.RUNTIMES)
def test_every_runtime_has_a_default_base_url(runtime):
    assert lm.default_base_url(runtime).startswith("http")


# --------------------------------------------------------------------------- #
# The probe: reachability + model discovery, degrading instead of raising.
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload if payload is not None else {}

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_probe_lists_ollama_models(monkeypatch):
    monkeypatch.setattr(
        lm.urllib.request,
        "urlopen",
        lambda url, timeout=0: _Resp(payload={"models": [{"name": "qwen:7b"}]}),
    )
    out = lm.probe(_cfg())
    assert out["running"] is True and out["models"] == ["qwen:7b"]


def test_probe_lists_openai_style_models(monkeypatch):
    monkeypatch.setattr(
        lm.urllib.request,
        "urlopen",
        lambda url, timeout=0: _Resp(payload={"data": [{"id": "local-model"}]}),
    )
    out = lm.probe(_cfg(runtime="lmstudio"))
    assert out["running"] is True and out["models"] == ["local-model"]


def test_probe_reports_a_dead_server_instead_of_raising(monkeypatch):
    def boom(url, timeout=0):
        raise lm.urllib.error.URLError("connection refused")

    monkeypatch.setattr(lm.urllib.request, "urlopen", boom)
    out = lm.probe(_cfg())
    assert out["running"] is False and "connection refused" in out["error"]


# --------------------------------------------------------------------------- #
# Launch-path integration: the overlay actually reaches the generated script.
# --------------------------------------------------------------------------- #
def test_provisioned_launcher_exports_env_and_flags(tmp_path, monkeypatch):
    """An ingested ticket on aider + Ollama must launch against localhost — env
    exported (so it survives the `exec`) and the model flag on EVERY relaunch in
    the resume chain, not just the first start."""
    monkeypatch.setattr(lm, "load_config", lambda: _cfg())
    path = provisioned.write_launcher(str(tmp_path), "Do the thing", program="aider")
    script = Path(path).read_text()
    # (The inner script is shlex-quoted into the outer `bash -ilc` wrapper, so
    # assert on the stable fragment rather than exact quoting.)
    assert "export OLLAMA_API_BASE=http://127.0.0.1:11434" in script
    # The flag rides on the launch program, so it appears in the first launch AND
    # in the resume line (twice more in the retry chain is fine — never zero).
    assert script.count("ollama_chat/qwen2.5-coder:7b") >= 2


def test_provisioned_launcher_unchanged_when_local_models_are_off(
    tmp_path, monkeypatch
):
    def _render(name: str) -> str:
        d = tmp_path / name
        d.mkdir()
        txt = Path(provisioned.write_launcher(str(d), "x", program="aider")).read_text()
        return txt.replace(str(d), "/WORKDIR")

    monkeypatch.setattr(lm, "load_config", lambda: LocalModelConfig())
    off = _render("a")
    monkeypatch.setattr(lm, "load_config", lambda: _cfg(enabled=False))
    disabled = _render("b")
    assert off == disabled
    assert "OLLAMA" not in off


def test_standalone_launch_command_carries_env_and_flags(monkeypatch):
    """The standalone tmux path (engine bridge off) must route locally too —
    otherwise the privacy guarantee depends on which launch mode you use."""
    from backend.providers import launch_script

    monkeypatch.setattr(lm, "load_config", lambda: _cfg(runtime="lmstudio"))
    preamble, command = launch_script.launch_command("aider", "/tmp/p.md")
    assert "export LM_STUDIO_API_BASE=" in preamble
    assert "lm_studio/qwen2.5-coder:7b" in command


def test_plain_session_relaunch_re_derives_the_overlay(monkeypatch):
    """A rebooted plain session rebuilds its command from Program/LaunchArgs,
    which never carried the overlay — so the relaunch path must re-derive it or a
    local-model session quietly returns to the CLI's hosted API."""
    from backend.providers import launch_script

    monkeypatch.setattr(lm, "load_config", lambda: _cfg())
    env, args = launch_script.local_overlay("aider")
    assert env and args  # the relaunch path applies exactly these
    exports = launch_script.env_exports(env)
    assert exports.startswith("export OLLAMA_API_BASE=")
    # It must end with a newline: the relaunch prepends it to a command that may
    # contain `||` chains, so it has to be its own statement.
    assert exports.endswith("\n")


def test_env_exports_is_sorted_and_quoted():
    from backend.providers import launch_script

    out = launch_script.env_exports({"B": "2", "A": "a b"})
    assert out.index("export A=") < out.index("export B=")
    assert "'a b'" in out
    assert launch_script.env_exports({}) == ""


def test_codex_local_flags_precede_the_seed_prompt(monkeypatch):
    """Order matters for codex: the prompt is POSITIONAL, so a flag emitted after
    it would be read as part of the prompt."""
    from backend.providers import launch_script

    monkeypatch.setattr(lm, "load_config", lambda: _cfg())
    _, command = launch_script.launch_command("codex", "/tmp/p.md")
    assert command.index("--oss") < command.index("$(cat")
