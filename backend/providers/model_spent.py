"""Models a plan has used up, and until when: a launch starts on the next one.

The model-downshift sweep (:mod:`backend.web.core.model_downshift`) reads
"your Fable limit resets Nov 30, 1am" off one session and switches THAT
session to Opus. Every other launch would still start on Fable and bounce off
the same notice before being switched: a new session, a worker spawned a
minute later, a restart, and a ``--resume`` (it starts on the CLI's saved
default, which the sweep puts back to the person's own). So the sweep records
the spent model here, and a launch whose model would be the spent one gets
the provider's flag for the next model down (``--model opus``) until it
resets.

Keyed by the CLI's default-model file (``BaseProvider.model_default_file``,
Claude's ``<config dir>/settings.json``): two accounts have two allowances. A
launch that already names a model (a ``--model`` in its launch args, an
account profile's model) is left alone. A notice whose reset can't be read
counts for :data:`UNKNOWN_RESET_S`.
"""

from __future__ import annotations

import os
import time
from typing import Dict, Optional, Sequence

from backend.fsutil import atomic_write_json, read_json

__all__ = ["record", "spent", "launch_model", "launch_args", "clear"]

STORE_FILE = "model-spent.json"
#: How long a spent model counts when its notice gave no readable reset.
UNKNOWN_RESET_S = 5 * 3600.0
#: Never trust a reset further out than this (a misread year).
MAX_HOLD_S = 8 * 86400.0


def _path() -> str:
    from backend import config

    return os.path.join(config.GetConfigDir(), STORE_FILE)


def _load(now: float) -> Dict[str, Dict[str, float]]:
    data = read_json(_path(), {}, expect=dict) or {}
    out: Dict[str, Dict[str, float]] = {}
    for key, models in data.items():
        if not isinstance(models, dict):
            continue
        keep = {}
        for m, until in models.items():
            try:
                if float(until) > now:
                    keep[str(m)] = float(until)
            except (TypeError, ValueError):
                continue
        if keep:
            out[str(key)] = keep
    return out


def record(
    key: str, model: str, reset_at: Optional[float], now: Optional[float] = None
) -> float:
    """Note ``model`` spent for ``key`` until ``reset_at``. Returns the
    until-time stored. Never raises."""
    now = time.time() if now is None else now
    until = reset_at if reset_at and reset_at > now else now + UNKNOWN_RESET_S
    until = min(until, now + MAX_HOLD_S)
    try:
        data = _load(now)
        models = data.setdefault(str(key), {})
        if abs(models.get(str(model).lower(), 0.0) - until) < 60.0:
            return until  # already noted (the sweep sees it every pass)
        models[str(model).lower()] = until
        atomic_write_json(_path(), data)
    except Exception:  # noqa: BLE001 — a launch hint, never a failure
        pass
    return until


def clear(key: Optional[str] = None) -> None:
    """Forget ``key``'s spent models (all keys when None). Never raises."""
    try:
        data = {} if key is None else _load(time.time())
        if key is not None:
            data.pop(str(key), None)
        atomic_write_json(_path(), data)
    except Exception:  # noqa: BLE001
        pass


def spent(key: str, now: Optional[float] = None) -> Dict[str, float]:
    """``{model: until}`` for ``key``, expired ones left out."""
    now = time.time() if now is None else now
    try:
        return _load(now).get(str(key), {})
    except Exception:  # noqa: BLE001
        return {}


def _family(value: object, ladder: Sequence[str]) -> Optional[str]:
    """The ladder model a CLI model value names: ``opus``,
    ``claude-opus-5-5``, ``opus[1m]`` all read as ``opus``."""
    v = str(value or "").strip().lower()
    if not v:
        return None
    for m in ladder:
        if m in v:
            return m
    return None


def _names_a_model(args: Sequence[str]) -> bool:
    return any(a == "--model" or str(a).startswith("--model=") for a in args or ())


def launch_model(
    provider, inst, args: Sequence[str] = (), now: Optional[float] = None
) -> Optional[str]:
    """The model a launch should start on instead of its default, or None
    (nothing spent, the launch names its own model, or every model below is
    spent too). The default is the one in the CLI's default file, else the
    top of the ladder (the plan's default: Claude Code starts on its best)."""
    try:
        ladder = [str(m).lower() for m in (provider.model_ladder() or ())]
    except Exception:  # noqa: BLE001
        return None
    if not ladder or _names_a_model(args):
        return None
    try:
        key = provider.model_default_file(inst)
    except Exception:  # noqa: BLE001
        key = None
    if not key:
        return None
    out = spent(key, now)
    if not out:
        return None
    cur = None
    data = read_json(key, None, expect=dict)
    if data:
        cur = _family(data.get("model"), ladder)
    cur = cur or ladder[0]
    if cur not in out:
        return None
    for m in ladder[ladder.index(cur) + 1 :]:
        if m not in out:
            return m
    return None


def launch_args(
    provider, inst, args: Sequence[str] = (), now: Optional[float] = None
) -> tuple:
    """The flags that start this launch on :func:`launch_model` (``()`` when
    none applies). Never raises."""
    try:
        m = launch_model(provider, inst, args, now)
        return tuple(provider.model_launch_args(m)) if m else ()
    except Exception:  # noqa: BLE001
        return ()
