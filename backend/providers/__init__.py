"""Coding-provider registry.

Resolve a :class:`CodingProvider` from an ``Instance.Program`` string (or an
explicit provider name). The default is ``claude``; new CLIs register here and
claim programs via their ``matches()`` predicate. The Claude provider's
``matches`` covers an empty program and ``claude`` — so an unconfigured
session behaves exactly as before.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .antigravity import AntigravityProvider
from .base import (
    BaseProvider,
    CodingProvider,
    LauncherSpec,
    LaunchContext,
    TrustSpec,
)
from .claude import ClaudeProvider
from .codex import CodexProvider
from .config import BUILTIN_CONFIGS, load_user_configs
from .generic import GenericProvider

# Imported (not just re-exported) so ``providers.launch_script`` /
# ``providers.local_models`` are always bound on the package — several launch
# paths reach them as attributes. Neither imports back into this module at
# module scope, so there is no cycle.
from . import launch_script, local_models  # noqa: E402

# Bundled config names whose behaviour needs a dedicated GenericProvider
# subclass (live usage / telemetry / resume-thread discovery). Anything not
# listed uses GenericProvider.
_CONFIG_PROVIDER_CLASSES = {"codex": CodexProvider, "antigravity": AntigravityProvider}

__all__ = [
    "BaseProvider",
    "CodingProvider",
    "GenericProvider",
    "LauncherSpec",
    "LaunchContext",
    "TrustSpec",
    "launch_script",
    "local_models",
    "register",
    "resolve",
    "normalize_program",
    "get",
    "all_providers",
    "rebuild_registry",
    "BUILTIN_NAMES",
    "DEFAULT_PROVIDER",
]


class _FallbackProvider(BaseProvider):
    """Catch-all for any program no named provider claims. Runs the program bare
    (resuming with ``--continue``) and uses the base trust/idle defaults — i.e.
    the generic fallback behaviour for a custom/unknown program."""

    name = "generic"

    def matches(self, program: str) -> bool:  # claims anything
        return True


DEFAULT_PROVIDER = "claude"

# Registration order is significant: ``resolve`` returns the first provider
# whose ``matches`` predicate accepts the program, so the generic catch-all (if
# any) must register last.
_REGISTRY: Dict[str, CodingProvider] = {}
_ORDER: List[str] = []


# resolve() runs its matcher loop on every call and is called several times
# per session per poll tick — memoized by program string, invalidated on any
# registry mutation.
_RESOLVE_CACHE: dict = {}


def register(provider: CodingProvider) -> CodingProvider:
    """Register ``provider`` under its ``name`` (idempotent; re-registers)."""
    if provider.name not in _REGISTRY:
        _ORDER.append(provider.name)
    _REGISTRY[provider.name] = provider
    _RESOLVE_CACHE.clear()
    return provider


def get(name: str) -> Optional[CodingProvider]:
    """Look up a provider by exact registry name."""
    return _REGISTRY.get(name)


def all_providers() -> List[CodingProvider]:
    """All registered providers in registration order."""
    return [_REGISTRY[n] for n in _ORDER]


def resolve(program_or_kind: Optional[str]) -> CodingProvider:
    """Resolve a provider from an ``Instance.Program`` string or provider name.

    1. exact provider-name match (when a persisted provider kind is passed),
    2. first provider whose ``matches(program)`` is True, in registration order,
    3. the default (``claude``).
    """
    key = (program_or_kind or "").strip()
    hit = _RESOLVE_CACHE.get(key)
    if hit is not None:
        return hit
    result = None
    if key in _REGISTRY:
        result = _REGISTRY[key]
    else:
        for name in _ORDER:
            try:
                if _REGISTRY[name].matches(key):
                    result = _REGISTRY[name]
                    break
            except (
                Exception
            ):  # noqa: BLE001 — a provider's matcher must never break resolution
                continue
    if result is None:
        result = _REGISTRY[DEFAULT_PROVIDER]
    if len(_RESOLVE_CACHE) < 256:  # program strings are user input — stay bounded
        _RESOLVE_CACHE[key] = result
    return result


def normalize_program(program: Optional[str]) -> str:
    """Canonical form of a program string: a provider NAME where one applies.

    ``"/opt/homebrew/bin/claude"`` -> ``"claude"``. A resolved absolute path is
    how :func:`backend.config.config.GetClaudeCommand` reports the CLI it found
    (it shells out to ``which``), and storing that verbatim leaks an install
    detail into every place a program is shown or matched — most visibly the New
    Session dialog, which lists any program it doesn't recognise as an extra
    dropdown entry, so a Homebrew Mac grew a mystery "/opt/homebrew/bin/claude"
    item above the real agents.

    Only a bare basename that a named provider claims is folded to that
    provider's name. Anything else — a custom script, a program with arguments,
    a path no provider recognises — is returned unchanged (stripped), because
    for those the exact string IS the launch command.
    """
    key = (program or "").strip()
    if not key or key in _REGISTRY:
        return key
    import os

    # Arguments mean the string is a command line, not a binary to identify.
    if len(key.split()) > 1:
        return key
    base = os.path.basename(key)
    if base == key:  # already a bare name
        return key
    for name in _ORDER:
        if name == "generic":
            continue  # the catch-all claims everything; it identifies nothing
        try:
            if _REGISTRY[name].matches(base):
                return name
        except Exception:  # noqa: BLE001 — a matcher must never break this
            continue
    return key


# --- built-in providers ----------------------------------------------------- #
# Order is significant: claude first (claims claude/empty), then the named
# CLIs, then any user-defined TOML configs, then the catch-all fallback LAST.

#: Names that ship with MindFlock (claude + BUILTIN_CONFIGS + the fallback). The
#: CRUD layer rejects edits/deletes of these — a user "override" of a builtin is
#: a user TOML that re-registers the same name via rebuild_registry().
BUILTIN_NAMES: List[str] = ["claude"] + [c.name for c in BUILTIN_CONFIGS] + ["generic"]


def rebuild_registry() -> None:
    """Rebuild the whole provider registry from scratch, in canonical order.

    Reconstructs: claude → bundled CLIs (aider/codex) → user TOML configs
    (``MINDFLOCK_PROVIDERS_DIR``) → the catch-all fallback LAST. Called at import
    and after any provider CRUD mutation so a newly written/removed user TOML is
    reflected immediately without a process restart, while resolution order is
    preserved by construction.
    """
    _REGISTRY.clear()
    _ORDER.clear()
    _RESOLVE_CACHE.clear()
    register(ClaudeProvider())
    for _cfg in BUILTIN_CONFIGS:
        cls = _CONFIG_PROVIDER_CLASSES.get(_cfg.name, GenericProvider)
        register(cls(_cfg))
    for _cfg in load_user_configs():
        register(GenericProvider(_cfg))
    register(_FallbackProvider())


rebuild_registry()
