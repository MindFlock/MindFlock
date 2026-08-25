""" "Put this window back to idle" — the owner's decision to restart the guided
cycle on a branch whose ladder is already finished.

WHY ANYTHING IS NEEDED. The workflow stage is derived from git on every pass
(:func:`backend.web.core.agent_state._session_stage`), and it self-heals: the
instant the tree goes dirty the stage is ``agent`` again, so "let me keep
working" needs no help there. The gap is the CLEAN-tree window — ``committed``,
``pushed``, ``pr`` — where the branch is finished as far as git can tell and the
header therefore insists on Push / Make PR / Merge. Someone who just opened a PR
and wants to carry on writing code on the same branch has no way to say so: every
control on the window is about advancing a cycle they consider done.

WHAT A PIN IS AND IS NOT. It is a per-session note that says "I know where the
ladder is; show me the start of it anyway". It changes NOTHING about git or
GitHub — the commits stay committed, the PR stays open — and it deliberately does
not touch the published ``stage`` either, so the autopilot driver, the
verification-check kicker and every ``*_changed`` event keep reading the same
git-derived truth they always did. Only the guided ladder in the UI (chip,
primary button, live step) honours the pin. A display pin that lied to the
autopilot could make an armed chain try to commit a clean tree.

HOW IT RELEASES ITSELF. Against the WORKTREE, not against the stage label. A pin
records the HEAD sha it was set on and dies the moment either
    * the tree goes dirty — new work exists, so reality already agrees, or
    * HEAD moves — a new commit landed, and its ladder is a real one to climb.
Keying release off the stage label instead cannot work: filing a PR moves the
stage ``pushed`` -> ``pr`` a beat after the pin is set, which would release it
immediately.

WHY IT IS NOT PERSISTED. The store is process memory. A pin is a statement about
this moment on this branch and costs one click to re-make, so losing pins to a
restart is a shrug; carrying a stale one across a restart — where the worktree
may have moved on in ways the recorded sha no longer describes — is worse.
:func:`prune` is still mandatory, because titles are REUSED after a delete and a
recreated session sitting on the same sha would otherwise inherit the pin.

Thread-safety follows the rest of ``core``: sync routes run in the worker
threadpool while the tick runs elsewhere, so every accessor takes the lock.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

__all__ = ["pin", "clear", "active", "get", "titles", "prune"]

_LOCK = threading.Lock()
#: title -> {"head": sha the pin was set on, "at": epoch}
_PINS: Dict[str, dict] = {}


def pin(title: str, head: str) -> bool:
    """Pin ``title``'s guided ladder back to the start. Returns whether it took.

    Refuses without a HEAD sha: the sha IS the release condition, and a pin with
    nothing to compare against could only be cleared by hand.
    """
    if not title or not head:
        return False
    with _LOCK:
        _PINS[title] = {"head": str(head), "at": time.time()}
    return True


def clear(title: str) -> bool:
    """Drop ``title``'s pin. Returns whether there was one."""
    if not title:
        return False
    with _LOCK:
        return _PINS.pop(title, None) is not None


def active(title: str, head: str, dirty: bool) -> bool:
    """Whether the pin still holds, releasing it in place when it does not.

    Called from the stage probe, which has just measured both arguments, so the
    release check costs nothing extra. Self-releasing on read is what keeps a
    dead pin from needing anyone to notice it died.
    """
    if not title:
        return False
    with _LOCK:
        rec = _PINS.get(title)
        if rec is None:
            return False
        if dirty or not head or head != rec.get("head"):
            _PINS.pop(title, None)
            return False
        return True


def get(title: str) -> Optional[dict]:
    """The raw pin record (diagnostics/tests), or None."""
    if not title:
        return None
    with _LOCK:
        rec = _PINS.get(title)
        return dict(rec) if rec else None


def titles() -> List[str]:
    """Pinned session titles (diagnostics/tests)."""
    with _LOCK:
        return list(_PINS.keys())


def prune(live_titles) -> int:
    """Drop pins for sessions that no longer exist. Returns how many went.

    Mandatory rather than housekeeping — see the module docstring on title
    reuse.
    """
    live = set(live_titles or ())
    with _LOCK:
        dead = [t for t in _PINS if t not in live]
        for t in dead:
            _PINS.pop(t, None)
    return len(dead)
