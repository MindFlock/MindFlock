"""Run a session's agent CLI against a LOCAL model server.

Why this exists: every bundled CLI otherwise talks to a hosted API, so running
MindFlock at all required a paid subscription or an API key, and the privacy
story stopped at "we don't send it anywhere extra" rather than "nothing leaves
the machine". Point the CLI at a model served on localhost and both change: no
subscription, and no prompt, diff or file ever crosses the network.

The design is a thin **overlay**, not a new provider. A local model is a property
of the *runtime*, not of the CLI: codex, aider and goose each already know how to
talk to Ollama or LM Studio, they just need the right env vars and flags. So
:func:`overlay_for` maps ``(provider, LocalModelConfig)`` to
``(env, launch_args)`` that every launch path applies, and the provider registry
stays untouched — which also means a user-defined provider TOML keeps working.

Per-CLI mappings are **verified against installed binaries**, not guessed:

* ``codex`` — ``--oss --local-provider {ollama|lmstudio} -m <model>``. Verified
  against codex-cli's ``--help``: ``--oss`` selects the open-source provider and
  ``--local-provider`` picks which local server serves it.
* ``aider`` — model prefix plus env, from aider's own bundled docs
  (``aider/website/docs/llms/{ollama,lm-studio}.md``): Ollama wants
  ``OLLAMA_API_BASE`` and an ``ollama_chat/<model>`` name; LM Studio wants
  ``LM_STUDIO_API_BASE`` (with ``/v1``) plus a non-empty ``LM_STUDIO_API_KEY``
  (an empty Bearer token makes the client fail) and an ``lm_studio/<model>``
  name.
* ``goose`` — ``GOOSE_PROVIDER`` / ``GOOSE_MODEL``, plus ``OLLAMA_HOST`` for
  Ollama. Verified against the strings in the goose binary.

A CLI without a mapping is reported as unsupported rather than launched with
invented flags — see :func:`supported`. Claude Code is deliberately absent: it
speaks only the Anthropic API, so pointing it at a local model needs a
translating proxy, which is a different feature with different failure modes.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

__all__ = [
    "RUNTIMES",
    "LocalModelConfig",
    "default_base_url",
    "supported",
    "overlay_for",
    "probe",
]

#: The local model servers MindFlock knows how to point a CLI at.
#:
#: ``ollama`` and ``lmstudio`` are the two the CLIs name natively. ``custom`` is
#: any other OpenAI-compatible server (llama.cpp's ``llama-server``, vLLM, a
#: LiteLLM proxy): it is driven through the OpenAI-compatible path, which is what
#: those servers expose.
RUNTIMES = ("ollama", "lmstudio", "custom")

#: Default endpoint per runtime — each project's own documented default, so a
#: user who has not changed anything only has to pick a runtime and a model.
_DEFAULT_BASE_URLS = {
    "ollama": "http://127.0.0.1:11434",
    "lmstudio": "http://127.0.0.1:1234/v1",
    "custom": "http://127.0.0.1:8000/v1",
}

#: How each runtime is reached for a liveness/model probe: the path appended to
#: the base URL, and the JSON key holding the model list.
_PROBE = {
    "ollama": ("/api/tags", "models", "name"),
    "lmstudio": ("/models", "data", "id"),
    "custom": ("/models", "data", "id"),
}

#: Seconds to wait on a probe. Short: it targets localhost, and the caller is a
#: settings screen or a doctor check that must not hang.
_PROBE_TIMEOUT = 3.0

#: Coding CLIs with a verified local-model route (see the module docstring).
_SUPPORTED = ("codex", "aider", "goose")


def default_base_url(runtime: str) -> str:
    """The documented default endpoint for ``runtime``."""
    return _DEFAULT_BASE_URLS.get((runtime or "").strip().lower(), "")


@dataclass(frozen=True)
class LocalModelConfig:
    """A local model server plus the model to run on it.

    ``enabled`` is the master switch: off means every launch path behaves exactly
    as before (no env, no extra flags). ``runtime`` is one of :data:`RUNTIMES`,
    ``base_url`` defaults per runtime, and ``model`` is the server's own model
    name (``qwen2.5-coder:7b``, ``lmstudio-community/…``) — MindFlock adds
    whichever prefix the CLI needs, so paste the name the server reports.
    """

    enabled: bool = False
    runtime: str = "ollama"
    base_url: str = ""
    model: str = ""

    def resolved_base_url(self) -> str:
        return (self.base_url or "").strip() or default_base_url(self.runtime)

    def is_configured(self) -> bool:
        """Whether this config can actually route a CLI: on, with a model."""
        return bool(self.enabled and self.model.strip() and self.runtime in RUNTIMES)


def supported(provider_name: str) -> bool:
    """Whether ``provider_name`` has a verified local-model route."""
    return (provider_name or "").strip().lower() in _SUPPORTED


def _openai_compatible_env(cfg: LocalModelConfig) -> Dict[str, str]:
    """Env for a generic OpenAI-compatible server (the ``custom`` runtime).

    The key is a placeholder on purpose: these servers ignore it, but a client
    that sends an empty ``Bearer`` header gets rejected before the request is
    even served.
    """
    return {
        "OPENAI_API_BASE": cfg.resolved_base_url(),
        "OPENAI_BASE_URL": cfg.resolved_base_url(),
        # Literal placeholder, not a credential — see above.
        "OPENAI_API_KEY": "mindflock-local",  # pragma: allowlist secret
    }


def overlay_for(
    provider_name: str, cfg: LocalModelConfig
) -> Tuple[Dict[str, str], Tuple[str, ...]]:
    """``(env, launch_args)`` that point ``provider_name`` at ``cfg``'s server.

    Returns ``({}, ())`` when the config is off/incomplete or the CLI has no
    verified route — callers apply the result unconditionally, so "no overlay"
    must mean "behave exactly as before".
    """
    name = (provider_name or "").strip().lower()
    if not cfg.is_configured() or not supported(name):
        return {}, ()
    runtime = cfg.runtime
    base = cfg.resolved_base_url()
    model = cfg.model.strip()

    if name == "codex":
        # codex has first-class local support; --local-provider only accepts the
        # two servers it implements, so a custom endpoint goes through its
        # OpenAI-compatible path instead of a flag it would reject.
        if runtime in ("ollama", "lmstudio"):
            return {}, ("--oss", "--local-provider", runtime, "-m", model)
        return _openai_compatible_env(cfg), ("-m", model)

    if name == "aider":
        if runtime == "ollama":
            return {"OLLAMA_API_BASE": base}, ("--model", f"ollama_chat/{model}")
        if runtime == "lmstudio":
            return (
                # The key must be non-empty even though LM Studio ignores it; this
                # is a literal placeholder, not a credential.
                {
                    "LM_STUDIO_API_BASE": base,
                    "LM_STUDIO_API_KEY": "mindflock-local",  # pragma: allowlist secret
                },
                ("--model", f"lm_studio/{model}"),
            )
        return _openai_compatible_env(cfg), ("--model", f"openai/{model}")

    if name == "goose":
        # goose is configured entirely through env; it has no model flag.
        env = {"GOOSE_PROVIDER": runtime, "GOOSE_MODEL": model}
        if runtime == "ollama":
            env["OLLAMA_HOST"] = base
        elif runtime == "lmstudio":
            env["GOOSE_PROVIDER"] = "lmstudio"
            env.update(_openai_compatible_env(cfg))
        else:
            env["GOOSE_PROVIDER"] = "openai"
            env.update(_openai_compatible_env(cfg))
        return env, ()

    return {}, ()


def probe(cfg: LocalModelConfig) -> dict:
    """Ask the configured server whether it is up, and which models it serves.

    Returns ``{"running": bool, "models": [str], "base_url": str, "error": str}``.
    Synchronous ``urllib`` with a short timeout, and every failure is reported in
    ``error`` rather than raised: this backs a settings screen and a doctor check,
    both of which must render a "not running" state instead of a traceback.
    """
    base = cfg.resolved_base_url()
    out = {"running": False, "models": [], "base_url": base, "error": ""}
    path, list_key, name_key = _PROBE.get(
        (cfg.runtime or "").strip().lower(), _PROBE["custom"]
    )
    if not base:
        out["error"] = "no base URL configured"
        return out
    url = base.rstrip("/") + path
    try:
        with urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT) as resp:  # noqa: S310
            if resp.status != 200:
                out["error"] = f"HTTP {resp.status} from {url}"
                return out
            payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.URLError as err:
        out["error"] = f"cannot reach {url}: {getattr(err, 'reason', err)}"
        return out
    except (TimeoutError, OSError, ValueError) as err:
        out["error"] = f"cannot read {url}: {err}"
        return out
    out["running"] = True
    entries = payload.get(list_key) if isinstance(payload, dict) else None
    models = []
    for entry in entries or ():
        if isinstance(entry, dict) and entry.get(name_key):
            models.append(str(entry[name_key]))
        elif isinstance(entry, str):
            models.append(entry)
    out["models"] = models
    return out


def load_config() -> LocalModelConfig:
    """The user's local-model settings, or a disabled config.

    Best-effort by design: this is read on every launch, so a missing or corrupt
    settings file must degrade to "local models off" (the pre-feature behaviour)
    rather than break the launch.
    """
    try:
        from backend.config import settings as _settings

        lm = _settings.load_settings().local_model
        return LocalModelConfig(
            enabled=bool(lm.enabled),
            runtime=(lm.runtime or "ollama").strip().lower(),
            base_url=lm.base_url or "",
            model=lm.model or "",
        )
    except Exception:  # noqa: BLE001 — never break a launch over settings
        return LocalModelConfig()


def launch_overlay(program: str) -> Tuple[Dict[str, str], Tuple[str, ...]]:
    """``(env, launch_args)`` for ``program`` under the user's local-model config.

    The single entry point the launch paths call. ``({}, ())`` when local models
    are off, unconfigured, or unsupported for this CLI.
    """
    cfg = load_config()
    if not cfg.is_configured():
        return {}, ()
    try:
        from . import resolve

        name = resolve(program).name
    except Exception:  # noqa: BLE001
        name = (program or "").strip()
    return overlay_for(name, cfg)


def unsupported_note(program: str) -> str:
    """A one-line explanation when local models are on but ``program`` can't use
    them, else ``""``. Surfaced by the settings screen and ``mindflock doctor`` so
    the combination fails loudly in the UI instead of silently running the CLI
    against its hosted API — which is the one outcome the privacy story cannot
    afford to be quiet about.
    """
    cfg = load_config()
    if not cfg.is_configured():
        return ""
    try:
        from . import resolve

        name = resolve(program).name
    except Exception:  # noqa: BLE001
        name = (program or "").strip()
    if supported(name):
        return ""
    return (
        f"{name or program!r} has no local-model route, so it will still use its "
        f"hosted API. Switch the session's agent to one of "
        f"{', '.join(_SUPPORTED)} to keep everything on this machine."
    )
