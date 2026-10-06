"""Session lineage: parent links, spawn limits and the ``base_ref`` fork point.

A session may name a ``Parent`` — the session that spawned it (an orchestrator
agent driving workers through the MindFlock MCP) or later adopted it — and
carries ``Spawned`` when an agent, not a human, created it. Both persist with
the instance (``backend.session.storage.InstanceData``); this module holds the
pure logic the routes apply to them:

* walking the live parent chain (depth, descendants, cycle checks) over the
  engine's in-memory registry — a ``Parent`` that is not a live title is a dead
  link and ends the walk, the same lazy-validity rule the snapshot row applies;
* the create-time spawn limits (children per parent, depth, total spawned),
  read at request time from an env var when one is set (an operator's
  override), else Settings → Agent orchestration (``general.agent_max_*`` in
  settings.json), else the built-in default — so either can change them
  without a restart;
* validating a ``base_ref`` / ``base_branch`` pair against a real repo before
  a create is accepted, so a typo is a 400 rather than an asynchronous
  ``session.create_failed``.

Every function takes the instances mapping explicitly (the routes pass
``ENGINE.instances`` while holding ``ENGINE.lock``), so none of them import the
server and all of them are unit-testable against plain dicts.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Mapping, Optional

#: Env knobs (read per request) and their defaults.
MAX_CHILDREN_ENV = "MINDFLOCK_MAX_CHILDREN"
MAX_SPAWN_DEPTH_ENV = "MINDFLOCK_MAX_SPAWN_DEPTH"
MAX_SPAWNED_ENV = "MINDFLOCK_MAX_SPAWNED"
DEFAULT_MAX_CHILDREN = 8
DEFAULT_MAX_SPAWN_DEPTH = 3
DEFAULT_MAX_SPAWNED = 24

# Seconds to wait on the short git probes that validate a base ref.
_GIT_PROBE_TIMEOUT = 15

# A ref is passed to git as one argv element, so quoting is not the concern —
# an option-shaped value and whitespace/control bytes are. Length is bounded
# so an error message can always echo it back.
_MAX_REF_LEN = 256
_BAD_REF_CHARS = re.compile(r"[\x00-\x20\x7f]")


#: The settings.json field (``general.<field>``) behind each env knob.
_SETTING_FOR = {
    MAX_CHILDREN_ENV: "agent_max_children",
    MAX_SPAWN_DEPTH_ENV: "agent_max_spawn_depth",
    MAX_SPAWNED_ENV: "agent_max_spawned",
}
#: Where a person changes the caps — named in every refusal, so an agent that
#: hits one can tell its user what to raise.
SETTINGS_PLACE = "Settings → Agent orchestration"


def _from_env(env_name: str) -> Optional[int]:
    """The non-negative integer in ``env_name``, else None (unset, malformed
    or negative — a bad value never silently disables the guard-rail)."""
    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _from_settings(env_name: str) -> Optional[int]:
    """The settings.json value behind ``env_name``'s knob, else None."""
    field = _SETTING_FOR.get(env_name)
    if not field:
        return None
    try:
        # Local import: settings is a leaf module, but lineage must stay
        # importable (and unit-testable) without the config layer loaded.
        from backend.config.settings import load_settings

        value = getattr(load_settings().general, field, None)
    except Exception:  # noqa: BLE001 — an unreadable store reads as unset
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def source(env_name: str) -> str:
    """Where ``env_name``'s cap currently comes from: ``"env"``,
    ``"settings"`` or ``"default"``."""
    if _from_env(env_name) is not None:
        return "env"
    if _from_settings(env_name) is not None:
        return "settings"
    return "default"


def limit(env_name: str, default: int) -> int:
    """The cap behind ``env_name``: the env var when set (an operator's
    override), else the user's setting, else ``default``.

    Read at call time (not import time), so a changed env or a saved setting
    applies to the next request. A malformed or negative value falls through
    to the next layer rather than silently disabling the guard-rail.
    """
    value = _from_env(env_name)
    if value is None:
        value = _from_settings(env_name)
    return default if value is None else value


def _limit_note(env_name: str, value: int) -> str:
    """How a refusal names the cap it hit: the env var when that set it, else
    the place in Settings to change it."""
    where = source(env_name)
    if where == "env":
        return "limit %s=%d" % (env_name, value)
    if where == "settings":
        return "limit %d, set in %s" % (value, SETTINGS_PLACE)
    return "limit %s=%d; raise it in %s" % (env_name, value, SETTINGS_PLACE)


def parent_of(inst) -> str:
    """The stored ``Parent`` of an instance ("" for a root or a stand-in)."""
    return str(getattr(inst, "Parent", "") or "")


def live_parent(instances: Mapping, title: str) -> str:
    """``title``'s parent when it is a live title in ``instances``, else ""."""
    inst = instances.get(title)
    if inst is None:
        return ""
    parent = parent_of(inst)
    if not parent or parent == title or parent not in instances:
        return ""
    return parent


def children_of(instances: Mapping, title: str) -> list:
    """Titles whose stored ``Parent`` is ``title`` (sorted, live ones only)."""
    if not title:
        return []
    return sorted(
        t for t, inst in instances.items() if t != title and parent_of(inst) == title
    )


def depth_of(instances: Mapping, title: str) -> int:
    """Hops from ``title`` up its live parent chain to a root (root = 0).

    A dead link ends the chain; a cycle (only reachable through hand-edited
    state, since the routes refuse to create one) ends it at the first repeat.
    """
    depth = 0
    seen = {title}
    current = title
    while True:
        parent = live_parent(instances, current)
        if not parent or parent in seen:
            return depth
        seen.add(parent)
        depth += 1
        current = parent


def ancestors_of(instances: Mapping, title: str) -> list:
    """The live parent chain above ``title``, nearest first (cycle-safe)."""
    out: list = []
    seen = {title}
    current = title
    while True:
        parent = live_parent(instances, current)
        if not parent or parent in seen:
            return out
        seen.add(parent)
        out.append(parent)
        current = parent


def parent_error(instances: Mapping, title: str, parent: str) -> Optional[str]:
    """Why ``title`` may not take ``parent`` as its parent, or None.

    ``parent`` must be a live session other than ``title`` itself, and must
    not be ``title``'s own descendant (adopting your own ancestor would close a
    loop in the chain every walk above relies on terminating).
    """
    if parent == title:
        return "a session cannot be its own parent"
    if parent not in instances:
        return "unknown parent session: %s" % parent
    if title in ancestors_of(instances, parent):
        return "%s is a descendant of %s — that would make a cycle" % (parent, title)
    return None


def spawn_limit_error(instances: Mapping, parent: str, spawned: bool) -> Optional[str]:
    """The create-time limit a new session would break, or None.

    Applied while the caller holds the registry lock, right before the title
    is claimed, so concurrent creates cannot both squeeze under a cap. With a
    ``parent``: its live children must stay within ``MINDFLOCK_MAX_CHILDREN``
    and the new session's depth (root = 0) within
    ``MINDFLOCK_MAX_SPAWN_DEPTH``. A ``spawned`` session (agent-created, with
    or without a parent) must also keep the live total of spawned sessions
    within ``MINDFLOCK_MAX_SPAWNED``. Each message names its knob.
    """
    if parent:
        max_children = limit(MAX_CHILDREN_ENV, DEFAULT_MAX_CHILDREN)
        n_children = len(children_of(instances, parent))
        if n_children >= max_children:
            return "session %s already has %d live children (%s)" % (
                parent,
                n_children,
                _limit_note(MAX_CHILDREN_ENV, max_children),
            )
        max_depth = limit(MAX_SPAWN_DEPTH_ENV, DEFAULT_MAX_SPAWN_DEPTH)
        depth = depth_of(instances, parent) + 1
        if depth > max_depth:
            return "a child of %s would be at depth %d (%s)" % (
                parent,
                depth,
                _limit_note(MAX_SPAWN_DEPTH_ENV, max_depth),
            )
    if spawned:
        max_spawned = limit(MAX_SPAWNED_ENV, DEFAULT_MAX_SPAWNED)
        n_spawned = sum(
            1 for inst in instances.values() if bool(getattr(inst, "Spawned", False))
        )
        if n_spawned >= max_spawned:
            return "%d agent-spawned sessions are already live (%s)" % (
                n_spawned,
                _limit_note(MAX_SPAWNED_ENV, max_spawned),
            )
    return None


def adopt_limit_error(instances: Mapping, title: str, parent: str) -> Optional[str]:
    """The spawn limit adopting ``title`` under ``parent`` would break, or None.

    Re-parenting is a second way to grow a parent's fan-out, so it answers to
    the same knobs as a create: the new parent's live children (not counting
    ``title`` itself, which may already be one) plus one must stay within
    ``MINDFLOCK_MAX_CHILDREN``, and every session in ``title``'s subtree must
    stay within ``MINDFLOCK_MAX_SPAWN_DEPTH`` once it hangs below ``parent``.
    Detaching (``parent == ""``) is never limited. Apply under the registry
    lock, like :func:`spawn_limit_error`.
    """
    if not parent:
        return None
    max_children = limit(MAX_CHILDREN_ENV, DEFAULT_MAX_CHILDREN)
    n_children = len([t for t in children_of(instances, parent) if t != title])
    if n_children >= max_children:
        return "session %s already has %d live children (%s)" % (
            parent,
            n_children,
            _limit_note(MAX_CHILDREN_ENV, max_children),
        )
    max_depth = limit(MAX_SPAWN_DEPTH_ENV, DEFAULT_MAX_SPAWN_DEPTH)
    depth = depth_of(instances, parent) + 1 + _subtree_height(instances, title)
    if depth > max_depth:
        return "adopting %s under %s would put a session at depth %d (%s)" % (
            title,
            parent,
            depth,
            _limit_note(MAX_SPAWN_DEPTH_ENV, max_depth),
        )
    return None


def _subtree_height(instances: Mapping, title: str) -> int:
    """Levels below ``title`` (a leaf = 0); cycle-safe."""
    best = 0
    stack = [(title, 0)]
    seen = {title}
    while stack:
        node, level = stack.pop()
        best = max(best, level)
        for child in children_of(instances, node):
            if child not in seen:
                seen.add(child)
                stack.append((child, level + 1))
    return best


def branch_taken_error(repo: str, branch: str) -> Optional[str]:
    """Why a NEW branch ``branch`` cannot be cut in ``repo``: it already exists.

    Asked synchronously at create time when a ``base_ref`` is given, so the
    caller hears a 409 instead of a 202 whose background Start then refuses
    the branch — and so nothing on that failure path ever goes near a branch a
    closed or paused namesake still owns. Blocking — call via
    ``asyncio.to_thread``. A probe that cannot run says nothing (the engine's
    own refusal is the backstop).
    """
    if not branch or _ref_shape_error("branch", branch) is not None:
        return None
    cp = _git(repo, "show-ref", "--verify", "--quiet", "refs/heads/" + branch)
    if cp.returncode == 0:
        return (
            "a branch named %s already exists (a closed or paused session may "
            "still hold it) — pick another session title" % branch
        )
    return None


def _ref_shape_error(kind: str, ref: str) -> Optional[str]:
    """Reject a ref git must never see: option-shaped, whitespace/control
    bytes, or absurdly long."""
    if (
        len(ref) > _MAX_REF_LEN
        or ref.startswith("-")
        or _BAD_REF_CHARS.search(ref) is not None
    ):
        return "invalid %s: %r" % (kind, ref[:_MAX_REF_LEN])
    return None


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    """``git -C repo args…`` with a short timeout; a timeout or a missing git
    reads as a failed probe (returncode != 0), never an exception."""
    try:
        return subprocess.run(
            ["git", "-C", repo, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_GIT_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(["git", *args], 1, stdout="")


def resolve_base_ref(repo: str, ref: str) -> str:
    """The full commit sha ``ref`` names in ``repo``, or "" when it names none.

    Blocking (one ``git rev-parse``) — call via ``asyncio.to_thread``.
    """
    if _ref_shape_error("base_ref", ref) is not None:
        return ""
    cp = _git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    sha = (cp.stdout or "").strip()
    return sha if cp.returncode == 0 and sha else ""


def base_ref_error(repo: str, base_ref: str, base_branch: str) -> Optional[str]:
    """Why a ``base_ref`` / ``base_branch`` pair cannot fork a session off
    ``repo``, or None.

    ``base_ref`` must resolve to a commit in ``repo``; ``base_branch`` (only
    meaningful with a ``base_ref``) must be a well-formed branch name — it need
    not exist locally, since it names the branch the fork point came from for
    diffing, which may live only on a remote. Blocking — call via
    ``asyncio.to_thread``.
    """
    if base_branch and not base_ref:
        return "base_branch requires base_ref"
    if not base_ref:
        return None
    err = _ref_shape_error("base_ref", base_ref)
    if err:
        return err
    if base_branch:
        err = _ref_shape_error("base_branch", base_branch)
        if err:
            return err
        cp = _git(repo, "check-ref-format", "--branch", base_branch)
        if cp.returncode != 0:
            return "invalid base_branch: %s" % base_branch
    if not resolve_base_ref(repo, base_ref):
        return "unknown base_ref: %s (no such commit in %s)" % (base_ref, repo)
    return None
