"""Red-zone reconcile loop: keeps every live worktree's guard file current,
re-arms the hook config when something disarms it, turns the tool feed and
the worktree's change set into ``session.red_zone_*`` events, and holds the
per-title summary the sidebar row shows.

WHY A LOOP AND NOT JUST THE HOOK. The hook (``providers/_tool_hook_src``)
enforces at fire time, but only from what is on disk: a guard file per
worktree root and a ``.claude/settings.local.json`` that runs it. Both can go
stale (a zone added from another tab, a branch commit that makes the push gate
relevant) or be tampered with (the agent rewriting its own hooks file, a
``disableAllHooks: true`` in the project settings). This loop is the other half:
every few seconds it re-derives what those files SHOULD say and puts it back,
and it is the only place that can notice what the hook cannot — a Bash write
that slipped past the heuristic, an edit to a git-ignored config file, a zone
store changed behind our back.

THE EVENT CONTRACT (what the critique insisted on, and why):

* **Seed silently on first observation.** A session's feed cursor starts at
  the newest record already on disk and a worktree's breach set starts as
  "already known", so a server restart — or a zone created over work already
  done — never re-announces standing state. That replaces the boot-quiet
  window the ``*_changed`` events use.
* **Blocked: once per (session, zone) per work cycle.** A denied agent usually
  retries a few ways before giving up; one push per attempt would be noise.
  The cycle resets when the session's activity reads idle/offline.
* **Breached: once per (worktree, path).** Breaches are a property of the
  worktree, not of whichever window noticed — two sessions sharing a worktree
  must not double-fire (the owner runs ``foo`` + ``foo-copy`` on one branch).
  Only VERIFIED changes count: git's change set, a git-ignored zoned file
  whose content left its first-sight baseline. A Bash-backstop feed record is
  evidence that triggers the check, never a breach on its own word.
* **Blocked covers pushes.** A refused ``git push`` / ``gh pr`` / GitHub-MCP
  write is a block too (``push: true``), keyed apart from zone blocks.
* **Tampered: only on a transition from armed.** A first arm (a pre-feature
  session picking up the tool hook) is silent; losing an arm we had is news.

COST. Everything runs on a worker thread (``asyncio.to_thread`` from the
server's ``_red_zone_loop``), never inside ``_instances_tick``. A worktree with
no zones costs a store read and a guard-file compare per tick; the git work
(change set, committed range) runs only for worktrees that HAVE zones, only
when the worktree fingerprint moves, and at most every 10 s.

``tick`` never raises: one bad worktree is skipped, the loop carries on.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat as _stat
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from backend.web.core import code_map
from backend.web.core import events as _events

_LOCK = threading.RLock()

#: Floor between two change-set recomputations for one worktree (the git
#: fingerprint gates it too — an unchanged worktree is never re-diffed).
BREACH_MIN_INTERVAL_S = 10.0
#: Feed trimming, snap/guard GC and the store tamper check.
HOUSEKEEPING_EVERY_S = 60.0
#: One tampered event per (title, what) per this many seconds — an agent that
#: keeps disarming the guard is one alert a minute, not one per tick.
TAMPER_COOLDOWN_S = 60.0
#: A tool call this long after arming with no feed record = "arming".
ARMING_GRACE_S = 2.0
#: Re-list the zone-matched git-ignored files this often (they are invisible
#: to ``git status``, so a new one only shows up on a re-list).
IGNORED_REFRESH_S = 60.0
_IGNORED_CAP = 500
_HASH_MAX_BYTES = 2 * 1024 * 1024
#: After a route writes the store, its own digest change is not tampering.
_ROUTE_GRACE_S = 5.0
#: A failed hook re-arm is retried at most this often.
_HEAL_RETRY_S = 30.0
#: Full guard re-sync at least this often even when nothing seems to have
#: moved (new files created inside a zone reach the Bash backstop's list).
GUARD_REFRESH_S = 30.0
#: A feed file no registered session owns (a manual ``claude`` in a checkout
#: that still carries MindFlock's hooks, the ``<tmux>_sh`` shell pane, a
#: session removed without the DELETE route) is deleted once it has been
#: quiet this long; until then it is size-trimmed like a live one.
ORPHAN_FEED_MAX_AGE_S = 86400.0
#: How long a DELETEd title stays tombstoned when the route could not name
#: its instance (the normal path prunes the tombstone as soon as the title
#: leaves the engine).
_TOMB_TTL_S = 120.0
_IDLE = ("idle", "offline")
#: Claude's ``EnterWorktree`` sandbox, nested inside the session's worktree.
#: The hook keys backstop breaches there as ``.claude/worktrees/<n>/<rel>``
#: and matches zones against ``<rel>``.
_NESTED_WT_RE = re.compile(r"^(\.claude/worktrees/[^/]+)/(.+)$")
#: The sentence a hooks file that won't parse puts in the guard pill.
HOOKS_INVALID_REASON = "hooks file is not valid JSON"
#: How the pill names a CLI (``provider.name`` is the program id).
_CLI_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "aider": "Aider",
    "opencode": "OpenCode",
    "antigravity": "Antigravity",
    "cline": "Cline",
    "goose": "Goose",
}

# title -> per-session state (feed cursor, work-cycle dedupe, row summary)
_SESS: Dict[str, dict] = {}
# worktree realpath -> per-worktree state (breaches, guard, hooks health)
_ROOTS: Dict[str, dict] = {}
# (title, what) -> last tampered emit
_TAMPER_AT: Dict[Tuple[str, str], float] = {}
# title -> (instance or None, forgotten_at): DELETEd, still registered while
# Kill runs — no tick may re-adopt it (see :func:`forget`).
_FORGOTTEN: Dict[str, Tuple[Any, float]] = {}
_HK = {"at": 0.0}
_STORE: Dict[str, Any] = {"digest": None, "snap": None, "route_at": 0.0}


def _server():
    """``backend.web.server``, imported lazily (it imports this module)."""
    from backend.web import server

    return server


def _rz():
    from backend.config import red_zones

    return red_zones


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _provider(inst: Any) -> Any:
    try:
        from backend import providers

        return providers.resolve(getattr(inst, "Program", "") or "")
    except Exception:  # noqa: BLE001
        return None


def _flag(prov: Any, name: str) -> bool:
    try:
        return bool(getattr(prov, name)())
    except Exception:  # noqa: BLE001
        return False


def _is_paused(inst: Any) -> bool:
    try:
        from backend.session.storage import Paused

        return inst.Status == Paused
    except Exception:  # noqa: BLE001
        return False


def _tmux_name(title: str) -> str:
    from backend.session import tmux

    return tmux.to_mindflock_tmux_name(title)


def _hooks_file(prov: Any, wt: str) -> str:
    """The hooks config a hot-reload provider reads. A provider may name its
    own (``hooks_settings_path(wt)``); Claude's is the only one that hot-
    reloads today, and MindFlock owns its ``settings.local.json``."""
    fn = getattr(prov, "hooks_settings_path", None)
    if callable(fn):
        try:
            p = fn(wt)
            if p:
                return str(p)
        except Exception:  # noqa: BLE001
            pass
    return os.path.join(wt, ".claude", "settings.local.json")


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _match(rules: List[dict], rel: str, ci: bool = False) -> Optional[dict]:
    """The first enforced zone matching ``rel`` (as ``{"pattern","zone_id"}``).

    ``ci`` = the worktree's filesystem is case-insensitive: the hook then
    matches with IGNORECASE, so a zone typed ``Config`` covers ``config/`` —
    and every server-side check has to agree with it or a change the hook
    would refuse is invisible here. A ``.claude/worktrees/<n>/`` prefix
    (Claude's nested sandbox checkout) is matched with and without, like the
    hook does: an edit there is an edit to the same logical file."""
    rz = _rz()
    cands = [rel]
    m = _NESTED_WT_RE.match(rel or "")
    if m:
        cands.append(m.group(2))
    for z in rules:
        if z.get("re") and any(rz.matches(z["re"], c, ci) for c in cands):
            return {"pattern": z.get("pattern"), "zone_id": z.get("id")}
    return None


def _verdict(doc: dict, rel: str, ci: bool = False) -> Optional[dict]:
    """``{"pattern", "zone_id", "kind"}`` when ``rel`` is a zone violation
    under ``doc`` (``red_zones.zones_doc``) — blocked by a red zone or outside
    the green scope — else None. The ONE predicate (``red_zones.classify``);
    exemptions are the caller's business (:func:`_breaches_of`)."""
    v = _rz().verdict(doc, rel or "", ci)
    if v is None:
        return None
    return {"pattern": v.get("pattern"), "zone_id": v.get("zone_id"), "kind": v["kind"]}


def _breaches_of(
    doc: dict, rels: Iterable[str], ci: bool, root: str, rev: Optional[str] = None
) -> Dict[str, dict]:
    """``{rel: verdict}`` — the paths of ``rels`` that are breaches: blocked,
    or outside and not exempt (an exempt path whose blob moved is one)."""
    out = _rz().breach_verdicts(doc, rels, ci, root=root, rev=rev)
    return {
        k: {"pattern": v.get("pattern"), "zone_id": v.get("zone_id"), "kind": v["kind"]}
        for k, v in out.items()
    }


def _doc_sig(doc: dict) -> tuple:
    """What a zone-set change means for the breach set: every red/green rule,
    the companion rules and the exemption set."""
    return (
        tuple(
            sorted((str(z.get("id")), str(z.get("re"))) for z in doc.get("red") or [])
        ),
        tuple(
            sorted((str(z.get("id")), str(z.get("re"))) for z in doc.get("green") or [])
        ),
        tuple(sorted(str(c.get("re")) for c in doc.get("companions") or [])),
        tuple(sorted((doc.get("exempt") or {}).items())),
    )


def _enforcing(doc: Optional[dict]) -> bool:
    return bool(doc) and bool(doc.get("red") or doc.get("green"))


def root_ci(root: str) -> bool:
    """Whether ``root``'s filesystem is case-insensitive — the same probe the
    guard file's ``ci`` comes from. False on any doubt. Never raises."""
    try:
        return bool(_rz()._probe_case_insensitive(os.path.realpath(root)))
    except Exception:  # noqa: BLE001
        return False


def _cli_label(prov: Any, inst: Any = None) -> str:
    name = getattr(prov, "name", "") or getattr(inst, "Program", "") or ""
    name = str(name)
    if not name:
        return "this CLI"
    return _CLI_LABELS.get(name) or (name[:1].upper() + name[1:])


def _ago(secs: float) -> str:
    secs = max(0.0, float(secs))
    if secs < 5:
        return "just now"
    if secs < 60:
        return "%ds ago" % int(secs)
    if secs < 3600:
        return "%d min ago" % int(secs // 60)
    if secs < 86400:
        return "%d h ago" % int(secs // 3600)
    return "%d d ago" % int(secs // 86400)


def _file_sig(path: str) -> Optional[tuple]:
    """``(size, sha1)`` of a file's content (first 2 MB + size for bigger
    ones), or None when it doesn't exist. Content, not mtime: an agent that
    edits a git-ignored zoned file and then REVERTS it (as the block reason
    tells it to) must stop counting as a breach."""
    try:
        st = os.stat(path)
        h = hashlib.sha1()
        with open(path, "rb") as f:
            h.update(f.read(_HASH_MAX_BYTES))
        return (st.st_size, h.hexdigest())
    except OSError:
        return None


def _stat_key(path: str) -> Optional[tuple]:
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return None


def _labels(items: Iterable[str], limit: int = 2) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    head = ", ".join(items[:limit])
    return head + (" and %d more" % (len(items) - limit) if len(items) > limit else "")


def _tampered(title: str, what: str, now: float, data: Optional[dict] = None) -> None:
    key = (title, what)
    with _LOCK:
        if now - _TAMPER_AT.get(key, 0.0) < TAMPER_COOLDOWN_S:
            return
        _TAMPER_AT[key] = now
    detail = {
        "guard": "the red-zone guard file was changed outside MindFlock — restored",
        "hooks": "the red-zone hook was removed or disabled — re-armed",
        "store": "the red-zone list was changed outside MindFlock",
    }.get(what, "red-zone protection was tampered with")
    payload = {"what": what, "detail": detail}
    payload.update(data or {})
    _events.BUS.emit("session.red_zone_tampered", session=title, data=payload)


# --------------------------------------------------------------------------- #
# State accessors
# --------------------------------------------------------------------------- #
def _sess(title: str) -> dict:
    with _LOCK:
        ss = _SESS.get(title)
        if ss is None:
            ss = {
                "cursor": None,
                "last_feed_ts": 0.0,
                "cycle": set(),
                "last_block_ts": None,
                "summary": None,
                # What the guard pill's sentence needs beyond the state.
                "guard_ctx": {},
            }
            _SESS[title] = ss
        return ss


def _root(root: str) -> dict:
    with _LOCK:
        rs = _ROOTS.get(root)
        if rs is None:
            rs = {
                "root": root,
                # Serializes a guard write against forget()'s remove_guard, so
                # a tick already in flight can't put a deleted guard back.
                "lock": threading.Lock(),
                "dead": False,
                "ci": None,
                "first_title": "",
                "repo_id": None,
                "seeded": False,
                "reseed": False,
                # The zone document in force before a zone-set change — the
                # reseed absorbs only what was NOT a breach under it.
                "reseed_doc": None,
                "doc": None,
                "sig": None,
                "fp": None,
                "checked_at": 0.0,
                "changed_hits": {},
                "committed": [],
                "ign_at": 0.0,
                "ign_base": {},
                "ign_hits": {},
                "current": {},
                "announced": set(),
                "armed": None,
                "armed_at": 0.0,
                "off_reason": "",
                "heal_at": 0.0,
                "guard_rules": False,
                "guard_key": None,
                "guard_want": None,
                "guard_at": 0.0,
            }
            _ROOTS[root] = rs
        return rs


# --------------------------------------------------------------------------- #
# The tick
# --------------------------------------------------------------------------- #
def tick(instances: Any, activity: Optional[Dict[str, str]] = None) -> None:
    """One reconcile pass over ``instances`` (``{title: Instance}``).

    ``activity`` is ``{title: "working"|"idle"|…}`` from the published sessions
    snapshot — used only to close a work cycle (the blocked-event dedupe), so a
    stale reading costs at most one suppressed or one extra notification.
    Never raises."""
    try:
        _tick(dict(instances or {}), dict(activity or {}))
    except Exception:  # noqa: BLE001 — the loop must never die
        pass


def _tombstoned(title: str, inst: Any, now: float) -> bool:
    """``title`` was DELETEd (its instance may stay registered for the seconds
    Kill takes): no tick may re-adopt it and rewrite its guard file."""
    with _LOCK:
        tomb = _FORGOTTEN.get(title)
    if not tomb:
        return False
    tinst, at = tomb
    if tinst is None:
        return now - at <= _TOMB_TTL_S
    return tinst is inst


def _live(instances: Dict[str, Any], now: Optional[float] = None) -> List[dict]:
    now = time.time() if now is None else now
    out = []
    for title, inst in instances.items():
        try:
            if _tombstoned(title, inst, now):
                continue
            if not inst.Started() or _is_paused(inst):
                continue
            wt = inst.GetWorktreePath()
        except Exception:  # noqa: BLE001
            continue
        if not wt or not os.path.isdir(wt):
            continue
        out.append(
            {
                "title": title,
                "inst": inst,
                "wt": wt,
                "root": os.path.realpath(wt),
                "tmux": _tmux_name(title),
                "prov": _provider(inst),
            }
        )
    return out


def _tick(instances: Dict[str, Any], activity: Dict[str, str]) -> None:
    now = time.time()
    with _LOCK:
        # A tombstone lives until its title leaves the engine (or a NEW
        # instance takes the title — titles are reused).
        for t in list(_FORGOTTEN):
            tinst, at = _FORGOTTEN[t]
            if (
                t not in instances
                or (tinst is not None and instances.get(t) is not tinst)
                or (tinst is None and now - at > _TOMB_TTL_S)
            ):
                _FORGOTTEN.pop(t, None)
    live = _live(instances, now)
    by_root: Dict[str, List[dict]] = {}
    for it in live:
        by_root.setdefault(it["root"], []).append(it)
    for root, items in by_root.items():
        try:
            _tick_root(root, items, activity, now)
        except Exception:  # noqa: BLE001 — one worktree can't stop the pass
            pass
    # Titles/roots that left without passing the DELETE route (a workspace
    # removed from Settings, a tombstone from another MindFlock).
    with _LOCK:
        for t in [t for t in _SESS if t not in instances]:
            _SESS.pop(t, None)
        for r in [r for r in _ROOTS if r not in by_root]:
            # Keep a root that merely paused (its title is still registered):
            # its breach baseline must survive a pause/resume.
            owners = [t for t, inst in instances.items() if _safe_root(inst) == r]
            if not owners:
                _ROOTS.pop(r, None)
    if now - _HK["at"] >= HOUSEKEEPING_EVERY_S:
        _HK["at"] = now
        try:
            _housekeeping(live, set(instances), now)
        except Exception:  # noqa: BLE001
            pass


def _safe_root(inst: Any) -> str:
    try:
        wt = inst.GetWorktreePath()
        return os.path.realpath(wt) if wt else ""
    except Exception:  # noqa: BLE001
        return ""


def _tick_root(root: str, items: List[dict], activity: Dict[str, str], now: float):
    rz = _rz()
    rs = _root(root)
    if rs["ci"] is None:
        rs["ci"] = root_ci(root)
    ci = bool(rs["ci"])
    titles = [it["title"] for it in items]
    if rs["first_title"] not in titles:
        rs["first_title"] = titles[0]
    owner = next(it for it in items if it["title"] == rs["first_title"])
    ident = rz.repo_identity(root)
    repo_id = ident[0] if ident else None
    if repo_id:
        rs["repo_id"] = repo_id
    zones = rz.effective_zones(owner["wt"], repo_id)
    doc = rz.zones_doc(owner["wt"], repo_id, zones=zones, ci=ci)

    bash: Dict[str, dict] = {}
    for it in items:
        try:
            bash.update(_feed_pass(it, doc, activity, now, ci))
        except Exception:  # noqa: BLE001
            pass

    # Each pass re-checks `dead`: a DELETE that lands mid-tick (forget runs on
    # another thread) must not see its guard rewritten, its hooks "healed" or
    # a tamper alert raised for the session the user just removed.
    # A failed identity lookup means repo zones are unknown this tick: keep the
    # previous breach state and guard file rather than recomputing against the
    # worktree zones alone (which would look like every repo zone vanished).
    if repo_id is not None and not rs["dead"]:
        _breach_pass(rs, owner, doc, bash, now, ci)
        _guard_pass(rs, owner, repo_id, now)
    if rs["dead"]:
        return
    _hooks_pass(rs, items, _enforcing(doc), now)
    for it in items:
        _summarize(it, rs, doc, now)


# --------------------------------------------------------------------------- #
# 1. Tool feed: blocked attempts + Bash backstop breaches
# --------------------------------------------------------------------------- #
def _feed_pass(
    it: dict,
    doc: dict,
    activity: Dict[str, str],
    now: float,
    ci: bool = False,
) -> Dict[str, dict]:
    title, tmux = it["title"], it["tmux"]
    ss = _sess(title)
    if ss["cursor"] is None:
        # Seed silently: everything already in the feed happened before we
        # were watching (a restart, or the session predates this process).
        recs = code_map.read_feed(tmux, 0.0, 1)
        newest = recs[-1].get("ts") if recs else None
        ok = isinstance(newest, (int, float)) and not isinstance(newest, bool)
        ss["cursor"] = float(newest) if ok else now
        ss["last_feed_ts"] = float(newest) if ok else 0.0
        return {}
    recs = code_map.read_feed(tmux, ss["cursor"], 500)
    tss = [
        float(r["ts"])
        for r in recs
        if isinstance(r.get("ts"), (int, float)) and not isinstance(r.get("ts"), bool)
    ]
    if tss:
        ss["cursor"] = max(ss["cursor"], max(tss))
        ss["last_feed_ts"] = max(ss["last_feed_ts"], max(tss))

    fresh: Dict[str, dict] = {}
    for r in recs:
        d = r.get("deny")
        if not isinstance(d, dict):
            continue
        ts = r.get("ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            ss["last_block_ts"] = max(ss["last_block_ts"] or 0.0, float(ts))
        # A refused `git push` / `gh pr` / GitHub-MCP write carries
        # ``push: true`` and names the first committed breach as its path. It
        # is its own dedupe key: blocking a push says nothing about which
        # ZONE the agent was reaching for, and must not swallow (or be
        # swallowed by) an edit block of the zone that path happens to be in.
        push = bool(d.get("push"))
        green = d.get("kind") == "green"
        if push:
            key = "push"
        elif green:
            # One scope-request event per work cycle, whichever file: the
            # Activity list carries each path with its [Allow this file].
            key = "green"
        else:
            key = str(
                d.get("zone_id") or "path:" + str(d.get("pattern") or d.get("path"))
            )
        if key in ss["cycle"]:
            continue
        ent = fresh.setdefault(
            key,
            {
                "zone_id": None if push else d.get("zone_id"),
                "pattern": None if push else d.get("pattern"),
                "label": (
                    ""
                    if push
                    else d.get("name") or d.get("pattern") or d.get("path") or ""
                ),
                "paths": [],
                "count": 0,
                "tool": "",
                "push": push,
                "kind": "green" if green else "red",
            },
        )
        ent["count"] += 1
        if d.get("path") and d["path"] not in ent["paths"]:
            ent["paths"].append(d["path"])
        ent["tool"] = r.get("tool") or ent["tool"]
    if fresh:
        ss["cycle"].update(fresh.keys())
        edits = [g for g in fresh.values() if not g["push"]]
        pushes = [g for g in fresh.values() if g["push"]]
        if edits:
            _emit_blocked(title, edits)
        if pushes:
            _emit_blocked(title, pushes, push=True)

    # Bash stat-diff backstop records. These are EVIDENCE that a command
    # touched a zoned path, not verdicts: _breach_pass checks each against
    # git and the content baseline before anything is announced.
    out: Dict[str, dict] = {}
    for r in recs:
        for b in r.get("breach") or []:
            if isinstance(b, dict) and b.get("path"):
                m = _verdict(doc, b["path"], ci) or {
                    "pattern": b.get("pattern"),
                    "zone_id": None,
                    "kind": b.get("kind") or "red",
                }
                out[str(b["path"])] = m
    # The work cycle ends when the agent stops; the next turn may announce the
    # same zone again (it is a new attempt the user hasn't heard about).
    if activity.get(title) in _IDLE:
        ss["cycle"].clear()
    return out


def _emit_blocked(title: str, groups: List[dict], push: bool = False) -> None:
    count = sum(g["count"] for g in groups)
    paths: List[str] = []
    for g in groups:
        for p in g["paths"]:
            if p not in paths:
                paths.append(p)
    green = [g for g in groups if g.get("kind") == "green"]
    if push:
        noun = "a push" if count == 1 else "%d pushes" % count
        detail = "blocked %s (zone breaches committed on this branch)" % noun
    elif green and len(green) == len(groups):
        noun = "an edit" if count == 1 else "%d edits" % count
        detail = "blocked %s outside the green zone(s): %s" % (
            noun,
            _labels(paths, 3) or "?",
        )
    else:
        labels = [g["label"] for g in groups]
        noun = "an edit" if count == 1 else "%d edits" % count
        detail = "blocked %s to %s" % (noun, _labels(labels) or "a red zone")
    _events.BUS.emit(
        "session.red_zone_blocked",
        session=title,
        data={
            "count": count,
            "zone_ids": [g["zone_id"] for g in groups if g["zone_id"]],
            "patterns": [g["pattern"] for g in groups if g["pattern"]],
            "paths": paths[:5],
            "tool": groups[-1]["tool"],
            "push": push,
            "kind": "green" if green and len(green) == len(groups) else "red",
            "detail": detail,
        },
    )


# --------------------------------------------------------------------------- #
# 2. Breaches (per WORKTREE)
# --------------------------------------------------------------------------- #
def _breach_pass(
    rs: dict,
    owner: dict,
    doc: dict,
    bash: Dict[str, dict],
    now: float,
    ci: bool = False,
) -> None:
    """Recompute the worktree's breach set and announce what is new.

    A BREACH is a changed path that :func:`red_zones.classify` calls
    ``blocked`` (a red zone) or ``outside`` (a green scope exists and the
    path is in none of it, nor a companion) and that is not an exemption
    still holding its recorded content. One predicate — the same the hook,
    the push gate and the Map use.

    THREE SOURCES, ONE TRUTH. The breach set is (a) git's change set against
    the fork point, (b) red-zone-matched git-ignored files whose CONTENT
    moved off their baseline (green ignores ignored files: they never ship),
    and (c) nested-sandbox paths git confirms changed. The Bash backstop's
    feed records only TRIGGER a recompute and supply evidence for (b); a
    backstop path neither git nor the content baseline confirms is dropped —
    a dir-mtime blip, a `touch`, a file created and deleted inside one
    command must never become a "breached" push.

    RESEED SCOPE. A zone-set change absorbs, silently, exactly the paths that
    were NOT breaches under the zone document in force before it (a new zone
    over finished work is not news) — ``breach_set(old)`` vs
    ``breach_set(new)``, never "matched by an old rule", which is backwards
    for green (a path an old GREEN rule matched was allowed). Baselines of
    ignored files survive the change, and a pending breach under the old
    document is still announced."""
    rz = _rz()
    inst, wt, root = owner["inst"], owner["wt"], rs["root"]
    rules = doc.get("red") or []
    sig = _doc_sig(doc)
    zones_changed = sig != rs["sig"]
    if zones_changed:
        if not rs["reseed"]:
            # A reseed still pending keeps its OLDER "before" document:
            # zone changes since were never processed either.
            rs["reseed_doc"] = rs["doc"]
        rs["sig"] = sig
        rs["reseed"] = True
    rs["doc"] = doc
    if not _enforcing(doc):
        rs.update(
            changed_hits={},
            committed=[],
            ign_hits={},
            ign_base={},
            ign_at=0.0,
            current={},
            fp=None,
            seeded=True,
            reseed=False,
            reseed_doc=None,
        )
        return

    fp = code_map.fingerprint(inst, wt)
    due = (
        zones_changed
        or bool(bash)
        or (
            (fp is None or fp != rs["fp"])
            and now - rs["checked_at"] >= BREACH_MIN_INTERVAL_S
        )
    )
    if due:
        changed = [c.get("path") or "" for c in code_map.changed_files(inst, wt)]
        hits = _breaches_of(doc, changed, ci, root)
        committed = sorted(
            _breaches_of(
                doc,
                code_map.committed_changed(inst, wt, "HEAD"),
                ci,
                root,
                rev="HEAD",
            )
        )
        rs.update(changed_hits=hits, committed=committed, checked_at=now)
        # The fingerprint from BEFORE the diff, not after it: a write that
        # lands while changed_files runs is then part of a fingerprint the
        # next tick sees move, which forces the re-diff that catches it (an
        # after-value would already include the write and hide it until
        # something else changed). The price is one extra diff after
        # changed_files' own `add -N` of a fresh untracked file.
        rs["fp"] = fp

    seeding = not rs["seeded"]
    # Red-zone-matched git-ignored files (invisible to git status): content
    # baseline, re-listed on a zone change, every IGNORED_REFRESH_S, and
    # whenever a backstop record names a path neither git nor the baseline
    # knows (the listing is what can vouch for it).
    unknown = {
        p
        for p, m in bash.items()
        if (m or {}).get("kind", "red") == "red"
        and p not in rs["changed_hits"]
        and p not in rs["ign_base"]
        and not _NESTED_WT_RE.match(p)
    }
    if rules and (zones_changed or unknown or now - rs["ign_at"] >= IGNORED_REFRESH_S):
        prev = rs["ign_base"]
        try:
            _f, _d, ignored, _t = rz.zone_files(root, rules)
        except Exception:  # noqa: BLE001
            ignored = list(prev.keys())
        ignored = ignored[:_IGNORED_CAP]
        listed = set(ignored)
        # "Absent from the last listing, present now" can only mean "created
        # since" when that listing covered the same zones and wasn't cut.
        prev_complete = (
            not seeding
            and rs["ign_at"] > 0
            and not zones_changed
            and len(prev) < _IGNORED_CAP
        )
        base: Dict[str, tuple] = {}
        if not seeding:
            for rel, v in prev.items():
                p = os.path.join(root, rel)
                if v[0] is None and not os.path.lexists(p):
                    continue  # created and removed again: nothing to hold
                # Kept while a zone still covers it, listed or not — a zoned
                # ignored file the agent DELETED is exactly a breach.
                if rel in listed or _match(rules, rel, ci):
                    base[rel] = v
        for rel in ignored:
            if rel in base:
                continue
            p = os.path.join(root, rel)
            if rel in unknown and prev_complete:
                # The hook saw an agent command create it, and the last full
                # listing of these zones didn't have it: "didn't exist".
                base[rel] = (None, None)
            else:
                # First sight: the baseline is the file as it is NOW. A build
                # artifact that appears in a zone (__pycache__, dist/) is not
                # something the agent changed.
                base[rel] = (_stat_key(p), _file_sig(p))
        rs["ign_base"] = base
        rs["ign_at"] = now
    elif not rules:
        rs.update(ign_base={}, ign_at=0.0)
    ign_hits: Dict[str, dict] = {}
    for rel, (skey, csig) in list(rs["ign_base"].items()):
        p = os.path.join(root, rel)
        cur_key = _stat_key(p)
        if cur_key == skey:
            continue
        cur_sig = _file_sig(p)
        if cur_sig == csig:
            rs["ign_base"][rel] = (cur_key, csig)  # touched, not changed
            continue
        m = _match(rules, rel, ci)
        if m:
            m["kind"] = "red"
            ign_hits[rel] = m
    rs["ign_hits"] = ign_hits

    current = dict(rs["changed_hits"])
    current.update(ign_hits)
    rs["current"] = current
    # Backstop paths inside Claude's nested sandbox checkout are invisible to
    # this worktree's git; that checkout's own git vouches for them.
    nested = {
        p: m
        for p, m in bash.items()
        if p not in current
        and _NESTED_WT_RE.match(p)
        and _verdict(doc, p, ci) is not None
        and _nested_changed(root, p)
    }
    seen = set(current) | set(nested)
    if seeding:
        rs["announced"] |= seen
        rs["seeded"] = True
        rs["reseed"] = False
        rs["reseed_doc"] = None
        return
    if rs["reseed"]:
        old = rs["reseed_doc"]
        fresh = seen - rs["announced"]
        if old is None or not _enforcing(old):
            was = {}
        else:
            was = _rz().breach_verdicts(old, fresh, ci, root=root)
        rs["announced"] |= {p for p in fresh if p not in was}
        rs["reseed"] = False
        rs["reseed_doc"] = None
    new = sorted(seen - rs["announced"])
    if not new or rs["dead"]:
        return
    rs["announced"] |= set(new)
    info = {p: (current.get(p) or nested.get(p) or {}) for p in new}
    patterns = sorted({str(m.get("pattern") or "") for m in info.values()} - {""})
    kinds = {str(m.get("kind") or "red") for m in info.values()}
    kind = "green" if kinds == {"green"} else "red"
    total = len(seen)
    noun = "a file" if len(new) == 1 else "%d files" % len(new)
    committed = set(rs["committed"])
    blocks_push = any(p in committed for p in new)
    if kind == "green":
        where = "outside the green zone(s)"
    else:
        where = "in red zone %s" % (
            _labels([p for p in patterns if p != rz.GREEN_OUTSIDE]) or "?"
        )
        if "green" in kinds:
            where += " / outside the green zone(s)"
    detail = "changed %s %s: %s — %s" % (
        noun,
        where,
        _labels(new, 3),
        _breach_consequence(new, committed, ign_hits),
    )
    _events.BUS.emit(
        "session.red_zone_breached",
        session=rs["first_title"],
        data={
            "paths": new[:5],
            "patterns": patterns,
            "total": total,
            "blocks_push": blocks_push,
            "kind": kind,
            "detail": detail,
        },
    )


def _breach_consequence(new: List[str], committed: Set[str], ign: Dict) -> str:
    """What the new breach actually does — the push gate reads COMMITTED
    changes only and an ignored file never leaves the machine, so "pushing is
    blocked" is true for exactly one of the three cases."""
    it = "it" if len(new) == 1 else "them"
    if any(p in committed for p in new):
        return "pushing is blocked until %s %s reverted" % (
            it,
            "is" if len(new) == 1 else "are",
        )
    if all(p in ign for p in new):
        return "git-ignored, so never pushed; restore %s by hand" % it
    return "committing %s would block pushes, PRs and merges" % it


def _nested_changed(root: str, rel: str) -> bool:
    """Whether ``.claude/worktrees/<n>/<inner>`` differs from that nested
    checkout's own HEAD/index (or is new and untracked) — git's view of a
    sandbox path the session worktree's git can't see. False on any doubt."""
    m = _NESTED_WT_RE.match(rel or "")
    if not m or ".." in m.group(2).split("/"):
        return False
    nested = os.path.join(root, m.group(1))
    if not os.path.exists(os.path.join(nested, ".git")):
        return False
    out = code_map._git(
        nested,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        ":(literal)" + m.group(2),
    )
    return bool(out and out.strip(b"\0"))


# --------------------------------------------------------------------------- #
# 3. Guard file
# --------------------------------------------------------------------------- #
_GUARD_ENFORCING_KEYS = (
    "rules",
    "green_rules",
    "companions",
    "protect",
    "sym",
    "files",
    "dirs",
)


def _same_enforcement(a: Optional[dict], b: Optional[dict]) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return all(a.get(k) == b.get(k) for k in _GUARD_ENFORCING_KEYS)


def _guard_pass(rs: dict, owner: dict, repo_id: str, now: float) -> None:
    rz = _rz()
    root = rs["root"]
    gp = rz.guard_path(root)
    # A full sync re-lists the zone's files (two `git ls-files`, one over the
    # ignored set) — too much for every 4 s on a big repo. Between syncs a
    # stat of the guard file is enough to notice a rewrite or a delete (an
    # atomic replace changes the inode), and anything that changes the
    # guard's CONTENT (zones, committed breaches, the session's path) forces
    # one; the periodic refresh picks up new files inside a zone.
    want = (rs["sig"], tuple(rs["committed"]), owner["wt"], repo_id)
    gkey = _stat_key(gp)
    if (
        gkey is not None
        and gkey == rs["guard_key"]
        and want == rs["guard_want"]
        and now - rs["guard_at"] < GUARD_REFRESH_S
    ):
        return
    # Under the root's lock, re-checking `dead`: forget() (the DELETE route,
    # another thread) takes the same lock to remove the guard, so a tick that
    # was already past _live when the delete landed can neither write the
    # guard back nor read its own restore as "a guard we had was deleted".
    with rs["lock"]:
        if rs["dead"]:
            return
        before = _read_json(gp)
        res = rz.sync_guard(
            root, repo_id, breaches=list(rs["committed"]), lroot=owner["wt"]
        )
        rs["guard_key"] = _stat_key(gp)
        rs["guard_want"] = want
        rs["guard_at"] = now
        after = _read_json(gp) if res in ("written", "healed") else before
        tampered = False
        if res == "healed":
            # "healed" = the file differed from what this process last wrote
            # while the store stood still. Another MindFlock process syncing
            # the same root (a CLI resume) differs only in breaches/lroot — not
            # tampering.
            tampered = not _same_enforcement(before, after)
        elif res == "written" and before is None and rs["guard_rules"]:
            tampered = True  # a guard we had written with rules was deleted
        if res in ("written", "healed", "unchanged"):
            a = after or {}
            rs["guard_rules"] = bool(a.get("rules") or a.get("green_rules"))
    if tampered:
        _tampered(rs["first_title"], "guard", now)


def committed_for(root: str) -> List[str]:
    """The last computed committed breaches for a worktree root (what the
    guard's ``breaches`` should keep saying across a route-triggered sync)."""
    with _LOCK:
        rs = _ROOTS.get(os.path.realpath(root) if root else "")
        return list(rs["committed"]) if rs else []


def resync(targets: Iterable[Tuple[str, str]], repo_id: Optional[str] = None) -> dict:
    """Sync guard files NOW for ``[(root, lroot), …]`` — called by the zone
    routes right after a store write so the change reaches the very next tool
    call instead of the next tick. Keeps each root's known committed breaches
    (a sync must not open the push gate). ``{root: result}``; never raises."""
    rz = _rz()
    out: Dict[str, str] = {}
    for root, lroot in targets:
        if not root:
            continue
        real = os.path.realpath(root)
        if real in out:
            continue
        try:
            out[real] = rz.sync_guard(
                real, repo_id, breaches=committed_for(real), lroot=lroot or real
            )
        except Exception:  # noqa: BLE001
            out[real] = "skipped"
    return out


# --------------------------------------------------------------------------- #
# 4. Hook health
# --------------------------------------------------------------------------- #
def _hooks_pass(rs: dict, items: List[dict], rules: Any, now: float) -> None:
    """Keep the hot-reload provider's hooks file armed. ``rules`` = whether
    any zone (red or green) is enforced here. A tool hook baked by an older
    build (its tag carries another source hash) is NOT armed: it is
    reinstalled, which is how a stale v1 guard — one that read green rules
    as red — heals without a relaunch."""
    cand = [
        it
        for it in items
        if _flag(it["prov"], "red_zone_guard") and _flag(it["prov"], "hooks_hot_reload")
    ]
    if not cand:
        rs["armed"] = None
        return
    from backend.providers import activity_markers as am

    it = cand[0]
    path = _hooks_file(it["prov"], it["wt"])
    raw: Optional[str] = None
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        raw = None  # absent: the heal below creates it
    except ValueError:
        raw = "\x00"  # not UTF-8: as unparsable as a trailing comma
    if raw is not None and raw.strip():
        try:
            valid = isinstance(json.loads(raw), dict)
        except ValueError:
            valid = False
        if not valid:
            # The user's own settings file, mid-edit or saved with a syntax
            # error. Healing would REPLACE it (a merge can't merge into what
            # it can't parse) and lose their permissions/env/hooks, and the
            # "tampered" alert would blame an attack for a typo. So: guard
            # off, with the reason on the pill, and no write, no alert. Once
            # it parses again the normal path below takes over.
            rs["armed"] = False
            rs["off_reason"] = HOOKS_INVALID_REASON
            return
    rs["off_reason"] = ""
    # "Tagged" = this build's tool hook (or a NEWER revision another build
    # installed) is in the file; an older one is a stale guard to replace,
    # zones or not.
    armed = am.hooks_armed(path)
    tagged = raw is not None and (armed or am.TOOL_HOOK_TAG in raw)
    # Another build's (older / same-revision) guard, still armed: a version
    # swap, not tampering — heal it at the retry pace, never alert.
    foreign = (
        not armed
        and raw is not None
        and am.TOOL_HOOK_TAG_PREFIX in raw
        and am.hooks_armed(path, any_version=True)
    )
    prev = rs["armed"]
    if (
        not armed
        and (rules or not tagged)
        and now - rs["heal_at"]
        >= (_HEAL_RETRY_S if (prev is False or foreign) else 0.0)
    ):
        rs["heal_at"] = now
        try:
            it["prov"].install_activity_hooks(it["wt"], it["tmux"])
        except Exception:  # noqa: BLE001
            pass
        healed = am.hooks_armed(path)
        if prev is True and not rs["dead"] and not foreign:
            # Armed last tick, disarmed now: someone removed the tool hook or
            # set disableAllHooks. A first arm (prev None/False) is silent.
            _tampered(rs["first_title"], "hooks", now)
        armed = healed
    rs["armed"] = armed
    if armed:
        try:
            rs["armed_at"] = os.stat(path).st_mtime
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 5. Row summary
# --------------------------------------------------------------------------- #
def _guard_state(it: dict, rs: dict, zones_n: int, now: float) -> str:
    prov = it["prov"]
    if zones_n <= 0:
        return "none"
    if not _flag(prov, "red_zone_guard"):
        return "detect"
    if not _flag(prov, "hooks_hot_reload"):
        return "guarded"  # armed at launch; nothing here can re-check it
    if not rs.get("armed"):
        return "off"
    ss = _sess(it["title"])
    armed_at = float(rs.get("armed_at") or 0.0)
    if ss["last_feed_ts"] >= armed_at:
        return "guarded"
    try:
        from backend.providers import activity_markers as am

        state = am.read_activity_marker(it["tmux"])
        age = am.read_activity_marker_age(it["tmux"])
    except Exception:  # noqa: BLE001
        state, age = None, None
    if state == "working" and age is not None:
        if now - age > armed_at + ARMING_GRACE_S:
            return "arming"  # a hook fired after arming, but no tool record
    return "guarded"


def _summarize(it: dict, rs: dict, doc: Any, now: float) -> None:
    ss = _sess(it["title"])
    if isinstance(doc, dict):
        zones_n = len(doc.get("red") or []) + len(doc.get("green") or [])
        mode = doc.get("mode")
    else:  # a bare rule list (older callers/tests)
        zones_n = len(doc or [])
        mode = "red" if zones_n else None
    ss["summary"] = {
        "zones": zones_n,
        "breaches": len(rs.get("current") or {}),
        "last_block_ts": ss["last_block_ts"],
        "guard": _guard_state(it, rs, zones_n, now),
        "mode": mode,
    }
    ss["guard_ctx"] = {
        "off_reason": rs.get("off_reason") or "",
        "armed_at": float(rs.get("armed_at") or 0.0),
        "hot": _flag(it["prov"], "hooks_hot_reload"),
        "hooks_file": _hooks_file_rel(it),
        "mode": mode,
    }


def _hooks_file_rel(it: dict) -> str:
    try:
        p = _hooks_file(it["prov"], it["wt"])
        rel = os.path.relpath(p, it["wt"])
        return p if rel.startswith("..") else rel
    except Exception:  # noqa: BLE001
        return ""


def summary(title: str) -> Optional[dict]:
    """The row's ``redzone`` field: ``{"zones", "breaches", "last_block_ts",
    "guard", "mode"}`` (``mode`` = "green" while a green scope is in force,
    "red" with red zones only, None) or None when there is nothing to say (no zones, no breaches,
    never blocked — or the loop hasn't seen the session yet). A pure
    in-memory read: safe on the snapshot path."""
    with _LOCK:
        ss = _SESS.get(title)
        s = dict(ss["summary"]) if ss and ss.get("summary") else None
    if s is None:
        return None
    if not s["zones"] and not s["breaches"] and s["last_block_ts"] is None:
        return None
    return s


def guard_detail(
    state: str,
    cli: str,
    ctx: Optional[dict] = None,
    last_feed_ts: float = 0.0,
    now: Optional[float] = None,
) -> str:
    """The guard pill's explanation: one full sentence saying what the state
    MEANS for this session (the pill itself carries the short label). The
    Map shows it as the pill's tooltip, so it must say what is and isn't
    prevented, not repeat the label."""
    ctx = ctx or {}
    now = time.time() if now is None else now
    armed_at = float(ctx.get("armed_at") or 0.0)
    if state == "guarded":
        if not ctx.get("hot", True):
            how = "hook installed at launch"
        elif last_feed_ts and last_feed_ts >= armed_at:
            how = "hook verified %s" % _ago(now - last_feed_ts)
        elif armed_at:
            how = "hook armed %s, not exercised yet" % _ago(now - armed_at)
        else:
            how = "hook armed"
        if ctx.get("mode") == "green":
            return (
                "%s is blocked before it edits a red zone or anything outside "
                "its green zone(s) (%s)" % (cli, how)
            )
        return "%s is blocked before it edits a red zone (%s)" % (cli, how)
    if state == "arming":
        since = " (armed %s)" % _ago(now - armed_at) if armed_at else ""
        return (
            "%s hasn't run the red-zone hook yet%s — until it does an edit can "
            "get through; one that does is still detected, flagged and blocks "
            "pushes" % (cli, since)
        )
    if state == "detect":
        if ctx.get("mode") == "green":
            return (
                "%s can't be stopped before an edit — changes outside the green "
                "zone(s) are detected, flagged and block pushes" % cli
            )
        return (
            "%s can't be stopped before an edit — red-zone changes are "
            "detected, flagged and block pushes" % cli
        )
    if state == "off":
        reason = ctx.get("off_reason") or ""
        if reason == HOOKS_INVALID_REASON:
            where = ctx.get("hooks_file") or ".claude/settings.local.json"
            return (
                "%s (%s) — MindFlock re-arms the guard as soon as it parses; "
                "until then edits are only detected and flagged" % (reason, where)
            )
        return "%s — MindFlock is re-arming the guard" % (
            reason or "The red-zone hook was removed or disabled"
        )
    if state == "none":
        return "No red zones apply to this worktree — add one to protect files"
    return str(state)


def guard_info(title: str, inst: Any, zones_n: int, mode: Optional[str] = None) -> dict:
    """``{"state", "detail", "hard"}`` for the Map's guard pill. ``detail`` is
    an explanatory sentence (:func:`guard_detail`), never just the label.
    Reads the loop's verdict when it has one; before the first tick (a
    just-created session, or a server that just started) derives a best
    guess."""
    prov = _provider(inst)
    hard = _flag(prov, "red_zone_guard")
    with _LOCK:
        ss = _SESS.get(title)
        state = (ss.get("summary") or {}).get("guard") if ss else None
        ctx = dict(ss.get("guard_ctx") or {}) if ss else {}
        last_feed_ts = float(ss.get("last_feed_ts") or 0.0) if ss else 0.0
    if mode is not None:
        ctx["mode"] = mode
    if not state:
        if zones_n <= 0:
            state = "none"
        elif not hard:
            state = "detect"
        else:
            state = "arming"
    detail = guard_detail(state, _cli_label(prov, inst), ctx, last_feed_ts)
    return {"state": state, "detail": detail, "hard": hard, "mode": ctx.get("mode")}


def ignored_breaches(root: str) -> Dict[str, dict]:
    """Red-zone-matched git-ignored files changed since their baseline —
    ``{rel: {"pattern", "zone_id", "kind"}}`` — the part of the breach set no git
    command can see (the live route adds it to the git-derived one)."""
    with _LOCK:
        rs = _ROOTS.get(os.path.realpath(root) if root else "")
        return {
            k: dict(v, kind="red") for k, v in (rs["ign_hits"] if rs else {}).items()
        }


# --------------------------------------------------------------------------- #
# Route hooks
# --------------------------------------------------------------------------- #
def seed_breaches(wt: str, paths: Iterable[str]) -> None:
    """Mark ``paths`` as already-known breaches of ``wt``'s worktree — the
    add-zone route's ``already_changed``, so work done before the zone existed
    is never announced as a breach of it."""
    if not wt:
        return
    root = os.path.realpath(wt)
    with _LOCK:
        rs = _root(root)
        # No reseed flag: the zone add that comes with this changes the zone
        # set, and THAT reseed absorbs whatever only the new zone covers. A
        # blanket reseed here would also swallow a pending breach of a zone
        # that was already there.
        rs["announced"] |= {p for p in paths or () if p}


def _store_snapshot() -> dict:
    data = _read_json(_rz().store_path()) or {}
    return {
        "repos": data.get("repos") if isinstance(data.get("repos"), dict) else {},
        "worktrees": (
            data.get("worktrees") if isinstance(data.get("worktrees"), dict) else {}
        ),
    }


def note_route_write() -> None:
    """A MindFlock route just wrote (or is about to write) the zone store: its
    digest change is ours, not tampering. Call it before AND after a write
    (:func:`route_write` does both) so a housekeeping pass that lands mid-write
    sees a fresh stamp."""
    try:
        dig = _rz().store_digest()
        snap = _store_snapshot()
    except Exception:  # noqa: BLE001
        dig, snap = None, None
    with _LOCK:
        _STORE["route_at"] = time.time()
        if dig is not None:
            _STORE["digest"] = dig
            _STORE["snap"] = snap


@contextlib.contextmanager
def route_write():
    """``with route_write(): red_zones.add_zone(...)`` — brackets a store
    mutation with :func:`note_route_write`."""
    note_route_write()
    try:
        yield
    finally:
        note_route_write()


def forget(
    title: str,
    wt: str,
    in_use_by_other: Optional[bool] = None,
    *,
    worktree_removed: bool = True,
    inst: Any = None,
) -> None:
    """Drop everything kept for ``title`` — for the instance DELETE path
    (titles are reused, and a namesake must not inherit a feed cursor, a work
    cycle, a plan latch or the old feed file). When no other instance uses
    the worktree, also its guard file and — only when ``worktree_removed`` —
    its worktree-scope zones/waivers. Never raises.

    ``worktree_removed=False`` is for a delete that leaves the folder on disk
    (an in-place session is the user's own checkout; Kill never removes it):
    its "This worktree" zones belong to the folder, not to the session, and
    the next session there must still be held by them. The DELETE route calls
    this before Kill with False and settles the zones in :func:`after_kill`,
    once it knows whether the folder is really gone.

    TOMBSTONE. The instance stays registered for the seconds Kill takes, so
    without one the very next tick would re-adopt the title, rewrite the
    guard file this just removed, and a tick already in flight would read its
    own restore as "a guard we had was deleted" — a tamper alert for the
    session the user just removed. ``inst`` scopes the tombstone to that
    instance, so a new session reusing the title is watched normally."""
    try:
        now = time.time()
        tmux = _tmux_name(title)
        with _LOCK:
            _FORGOTTEN[title] = (inst, now)
            _SESS.pop(title, None)
            for k in [k for k in _TAMPER_AT if k[0] == title]:
                _TAMPER_AT.pop(k, None)
        code_map.forget_plan(tmux)
        drop_feed(title)
        if not wt:
            return
        if in_use_by_other is None:
            try:
                in_use_by_other = bool(_server()._worktree_in_use_by_other(wt, title))
            except Exception:  # noqa: BLE001
                in_use_by_other = True  # unsure: keep the shared state
        if in_use_by_other:
            return
        root = os.path.realpath(wt)
        with _LOCK:
            rs = _ROOTS.pop(root, None)
        with rs["lock"] if rs is not None else contextlib.nullcontext():
            if rs is not None:
                rs["dead"] = True
                rs["guard_rules"] = False
            _rz().remove_guard(root)
        if worktree_removed:
            with route_write():
                _rz().forget_worktree(wt)
    except Exception:  # noqa: BLE001
        pass


def drop_feed(title: str) -> None:
    """Unlink ``title``'s tool-feed file. Never raises."""
    try:
        os.unlink(_rz().feed_path(_tmux_name(title)))
    except Exception:  # noqa: BLE001
        pass


def after_kill(title: str, wt: str, in_use_by_other: bool) -> None:
    """The DELETE route's second half, after Kill: unlink the feed AGAIN (a
    Post hook of the dying agent can recreate it between :func:`forget` and
    Kill), and — when no other session uses the worktree and Kill really
    removed the folder — its worktree-scope zones and waivers. A folder that
    survived (an in-place checkout, a failed removal) keeps them. Never
    raises."""
    drop_feed(title)
    try:
        if not wt or in_use_by_other or os.path.isdir(wt):
            return
        with route_write():
            _rz().forget_worktree(wt)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #
def _housekeeping(live: List[dict], registered: Set[str], now: float) -> None:
    rz = _rz()
    for it in live:
        try:
            code_map.trim_feed(it["tmux"])
        except Exception:  # noqa: BLE001
            pass
    try:
        _gc_feeds(registered, {it["tmux"] for it in live}, now)
    except Exception:  # noqa: BLE001
        pass
    try:
        code_map.gc_snaps()
    except Exception:  # noqa: BLE001
        pass
    try:
        rz.gc_guards({it["root"] for it in live})
    except Exception:  # noqa: BLE001
        pass
    with _LOCK:
        for k in [
            k for k, at in _TAMPER_AT.items() if now - at > 4 * TAMPER_COOLDOWN_S
        ]:
            _TAMPER_AT.pop(k, None)
    _store_check(live, now)


def _gc_feeds(registered: Set[str], trimmed: Set[str], now: float) -> int:
    """Bound the feed files no registered session owns. MindFlock never takes
    its hooks back out of a checkout, so an in-place repo keeps feeding
    ``<tmux session>.jsonl`` for any later manual ``claude`` run, the shell
    pane writes ``<tmux>_sh.jsonl``, and a session removed without the DELETE
    route leaves its file behind — none of which the per-session trim ever
    reaches. Quiet for ORPHAN_FEED_MAX_AGE_S → deleted; otherwise size-
    trimmed like a live one. A paused session is still registered, so its
    feed is kept. Returns how many files were deleted."""
    rz = _rz()
    keep = set()
    for t in registered:
        try:
            keep.add(os.path.basename(rz.feed_path(_tmux_name(t))))
        except Exception:  # noqa: BLE001
            continue
    done = {os.path.basename(rz.feed_path(t)) for t in trimmed}
    removed = 0
    with os.scandir(rz.feed_dir()) as entries:
        for e in entries:
            if not e.name.endswith(".jsonl") or e.name in keep:
                continue
            try:
                st = e.stat(follow_symlinks=False)
                if now - st.st_mtime > ORPHAN_FEED_MAX_AGE_S or not _stat.S_ISREG(
                    st.st_mode
                ):
                    os.unlink(e.path)
                    removed += 1
                elif e.name not in done:
                    code_map.trim_feed(e.name[: -len(".jsonl")])
            except Exception:  # noqa: BLE001
                continue
    return removed


def _store_check(live: List[dict], now: float) -> None:
    """Tamper detection for the zone store: its digest moved and no route of
    ours wrote it. Reported, never reverted — the user may have edited the
    file by hand on purpose; the alert is what lets them tell."""
    rz = _rz()
    dig = rz.store_digest()
    with _LOCK:
        last = _STORE["digest"]
        prev_snap = _STORE["snap"]
        recent_route = now - _STORE["route_at"] < _ROUTE_GRACE_S
        if last is None or dig == last or recent_route:
            _STORE["digest"] = dig
            _STORE["snap"] = _store_snapshot()
            return
    snap = _store_snapshot()
    with _LOCK:
        _STORE["digest"] = dig
        _STORE["snap"] = snap
    prev_snap = prev_snap or {"repos": {}, "worktrees": {}}
    repos = set(prev_snap["repos"]) | set(snap["repos"])
    changed_repos = {
        r for r in repos if prev_snap["repos"].get(r) != snap["repos"].get(r)
    }
    wts = set(prev_snap["worktrees"]) | set(snap["worktrees"])
    changed_wts = {
        w for w in wts if prev_snap["worktrees"].get(w) != snap["worktrees"].get(w)
    }
    hit = []
    for it in live:
        rs = _ROOTS.get(it["root"]) or {}
        if rs.get("repo_id") in changed_repos or it["root"] in changed_wts:
            hit.append(it["title"])
    if not hit:
        _tampered("", "store", now)
        return
    for title in hit:
        _tampered(title, "store", now)


def reset_for_tests() -> None:
    """Forget all in-memory state (tests only)."""
    with _LOCK:
        _SESS.clear()
        _ROOTS.clear()
        _TAMPER_AT.clear()
        _FORGOTTEN.clear()
        _HK["at"] = 0.0
        _STORE.update(digest=None, snap=None, route_at=0.0)
    try:
        _rz()._TESTS_MEMO.clear()
    except Exception:  # noqa: BLE001
        pass
