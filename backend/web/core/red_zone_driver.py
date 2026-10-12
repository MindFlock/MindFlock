"""The red-zone helpers behind the zone routes and the push/PR gate.

Policy (the zone store, the guard file, the one breach predicate) lives in
:mod:`backend.config.red_zones`; the reconcile loop and its events in
:mod:`backend.web.core.red_zone_monitor`. This module is what the routes in
server.py compute with:

* ``_rz_*`` — a worktree's repo identity and zones doc, the session's
  changed paths, green-scope exemptions and the guard-file resync a zone
  change needs (:func:`_live_repo_roots`, :func:`_rz_resync_after`).
* :func:`_live_breaches`, :func:`_uncommitted_zone_breaches`,
  :func:`_red_zone_breaches_for` — the breaches in the working tree, in what
  a commit would take, and committed on the way to a push/PR target
  (:func:`_pr_committed_changed`, :func:`_pr_content_ref`).
* the route helpers: shared (committed) zones, :func:`_green_preview`,
  :func:`_clean_rel_arg`, :func:`_sessions_on_worktree`.

The routes and the push/PR gate itself (``_red_zone_gate``) stay in
server.py. Server-owned names (``ENGINE``, the git probes tests patch on
``server``, ``_run_capped``) and ``_live_repo_roots`` (patched on
``server``) are reached through :func:`_server`; server.py re-imports every
name defined here.
"""

from __future__ import annotations

import os
import subprocess
from typing import Dict, Optional

from backend import providers
from backend.config import red_zones as _red_zones
from backend.config import repo_zones as _repo_zones
from backend.web.core import code_map as _code_map
from backend.web.core import person as _person
from backend.web.core import red_zone_monitor as _red_zone_monitor


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import)."""
    from backend.web import server

    return server


_RZ_SYNC_CAP = 50


def _rz_repo(wt: str):
    """``(repo_id, {"id", "label"})`` for a worktree, ``(None, None)`` when git
    can't say (the UI then shows worktree-scope zones only)."""
    ident = _red_zones.repo_identity(wt)
    if not ident:
        return None, None
    return ident[0], {"id": ident[0], "label": ident[1]}


def _rz_doc(wt: str, repo_id: Optional[str], zones=None, ci=None) -> dict:
    """``red_zones.zones_doc`` — what every zone check here classifies
    against (red, green, companions, exemptions)."""
    return _red_zones.zones_doc(wt, repo_id, zones=zones, ci=ci)


def _rz_hard(inst) -> bool:
    """Whether ``inst``'s CLI is hard-guarded (the hook blocks before an
    edit) — the agent messages say "blocked" vs "flagged and block pushes"."""
    try:
        prov = providers.resolve(getattr(inst, "Program", "") or "")
        return bool(prov.red_zone_guard())
    except Exception:  # noqa: BLE001
        return False


def _rz_changed_paths(inst, wt: str) -> tuple:
    """``(changed, committed)`` path lists of the session (working + committed
    vs its fork point; committed ``fork..HEAD``)."""
    changed = [c.get("path") or "" for c in _code_map.changed_files(inst, wt)]
    committed = _code_map.committed_changed(inst, wt, "HEAD")
    return [p for p in changed if p], list(committed)


def _rz_exempt_ids(root: str, paths: list) -> dict:
    """``{rel: "sha [sha …]"}`` — every identity a path had when it was
    exempted: its working-tree blob AND its blob at ``HEAD`` and at
    ``origin/<branch>`` (when pushed). The monitor compares the working
    tree, the push gate the blob at ``HEAD`` and the PR gate the one at
    ``origin/<branch>``: recording only the working tree blocked the push of
    work committed before the scope and then edited further."""
    if not paths:
        return {}
    wt_b = _red_zones.worktree_blobs(root, paths)
    revs = [_red_zones.rev_blobs(root, "HEAD", paths)]
    branch = _server()._current_branch(root) or ""
    if branch:
        ref = _pr_content_ref(root, branch)
        if ref != "HEAD":
            revs.append(_red_zones.rev_blobs(root, ref, paths))
    out = {}
    for p in paths:
        if p not in wt_b:
            continue
        ids = [wt_b[p]]
        for rb in revs:
            v = rb.get(p)
            if v and v not in ids:
                ids.append(v)
        out[p] = " ".join(ids)
    return out


def _rz_green_exempt(inst, wt: str, old_doc: dict, new_doc: dict) -> tuple:
    """Exempt the work a green scope change would suddenly call a breach.

    Every path already changed (working tree or committed) that is OUTSIDE
    under ``new_doc`` but was NOT a breach under ``old_doc`` is recorded with
    its current blob (``green_exempt``): the monitor and the push/PR gate
    skip it while its content stays the same. Scoping a session mid-flight —
    or narrowing its scope — must not block its push on work that was
    legitimate when it was done; editing such a file AFTER the scope exists
    is a breach again. ``(exempt paths, committed_outside count)``."""
    ci = bool(new_doc.get("ci"))
    changed, committed = _rz_changed_paths(inst, wt)
    root = os.path.realpath(wt)
    cand = sorted(set(changed) | set(committed))
    now_out = [
        p
        for p in cand
        if (_red_zones.verdict(new_doc, p, ci) or {}).get("kind") == "green"
    ]
    was = (
        _red_zones.breach_verdicts(old_doc, now_out, ci, root=root)
        if (old_doc.get("red") or old_doc.get("green"))
        else {}
    )
    fresh = [p for p in now_out if p not in was]
    if not fresh:
        return [], 0
    mapping = _rz_exempt_ids(root, fresh)
    if mapping:
        _red_zones.set_green_exempt(wt, mapping)
    csets = set(committed)
    return sorted(mapping), sum(1 for p in mapping if p in csets)


def _live_repo_roots(repo_id: Optional[str], extra_from: str = "") -> list:
    """``[(root, lroot)]`` of every worktree a repo-scope zone change must
    reach NOW: each live session's worktree with that repo id, plus (with
    ``extra_from``) that repo's own ``git worktree list`` — a checkout MindFlock
    isn't running can still carry an armed hook from an earlier session."""
    srv = _server()
    out, seen = [], set()

    def _add(p: str) -> None:
        if not p or not os.path.isdir(p) or len(out) >= _RZ_SYNC_CAP:
            return
        real = os.path.realpath(p)
        if real not in seen:
            seen.add(real)
            out.append((real, p))

    if not repo_id:
        return out
    for _t, other in list(srv.ENGINE.instances.items()):
        try:
            if not other.Started():
                continue
            owt = other.GetWorktreePath()
        except Exception:  # noqa: BLE001
            continue
        if not owt or not os.path.isdir(owt):
            continue
        ident = _red_zones.repo_identity(owt)
        if ident and ident[0] == repo_id:
            _add(owt)
    if extra_from:
        cp = srv._run_capped(
            ["git", "-C", extra_from, "worktree", "list", "--porcelain"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        if cp.returncode == 0:
            for line in cp.stdout.decode("utf-8", "replace").splitlines():
                if line.startswith("worktree "):
                    _add(line[len("worktree ") :].strip())
    return out


def _rz_resync_after(res: Optional[dict], wt: str = "") -> None:
    """Re-sync the guard files a removed/added zone reaches (``res`` is
    ``remove_zone``'s ``{"zone", "scope", "owner"}``)."""
    if not res:
        return
    if res.get("scope") == "repo":
        _red_zone_monitor.resync(
            _server()._live_repo_roots(res.get("owner"), wt), res.get("owner")
        )
    else:
        owner = res.get("owner") or ""
        lroot = wt if wt and os.path.realpath(wt) == owner else owner
        _red_zone_monitor.resync([(owner, lroot)])


def _live_breaches(inst, wt: str, changed, doc, ci: bool = False) -> list:
    """``[{"path", "pattern", "zone_id", "committed", "kind"}]``: the
    session's changed files that are breaches under ``doc`` — inside an
    enforced red zone, or outside the green scope and not exempt
    (``pattern`` = "outside green") — ``committed`` = already in a commit,
    i.e. what blocks a push; plus red-zone-matched git-ignored files the
    monitor saw change. Computed per request, never from the throttled row
    summary. ``ci``: the worktree's filesystem is case-insensitive, so match
    the way the hook does (IGNORECASE). ``doc`` may also be a bare list of
    enforced red zones (older callers)."""
    if isinstance(doc, list):
        doc = {"red": doc, "green": [], "companions": [], "exempt": {}}
    hits: Dict[str, dict] = {}
    rels = [c.get("path") or "" for c in changed or []]
    for rel, v in _red_zones.breach_verdicts(
        doc, rels, ci, root=os.path.realpath(wt)
    ).items():
        hits[rel] = {
            "path": rel,
            "pattern": v.get("pattern"),
            "zone_id": v.get("zone_id"),
            "committed": False,
            "kind": v.get("kind") or "red",
        }
    if hits:
        for rel in _code_map.committed_changed(inst, wt, "HEAD"):
            if rel in hits:
                hits[rel]["committed"] = True
    for rel, m in _red_zone_monitor.ignored_breaches(wt).items():
        if rel not in hits:
            hits[rel] = {
                "path": rel,
                "pattern": m.get("pattern"),
                "zone_id": m.get("zone_id"),
                "committed": False,
                "kind": "red",
            }
    return [hits[k] for k in sorted(hits)]


def _shared_zones_body(wt: str) -> dict:
    """``{"shared", "shared_error", "shared_path", "shared_exists"}`` — the
    committed zones of ``wt`` (backend.config.repo_zones)."""
    zones, error = _repo_zones.load_doc(wt)
    return {
        "shared": zones,
        "shared_error": error,
        "shared_path": _repo_zones.REL_PATH,
        "shared_exists": os.path.isfile(_repo_zones.file_path(wt)),
    }


def _shared_zone_author() -> str:
    """Who a shared zone says added it: the person's name, else their git
    name (docs/people.md). ``""`` when neither is set."""
    try:
        me = _person.current()
    except Exception:  # noqa: BLE001
        return ""
    return str(me.get("name") or me.get("git_name") or "").strip()


def _sessions_on_worktree(wt: str) -> int:
    """How many registered sessions share ``wt`` — a green zone is worktree
    scope, so it applies to all of them (the add row says so)."""
    real = os.path.realpath(wt)
    n = 0
    for _t, other in list(_server().ENGINE.instances.items()):
        try:
            owt = other.GetWorktreePath()
        except Exception:  # noqa: BLE001
            continue
        if owt and os.path.realpath(owt) == real:
            n += 1
    return n


def _clean_rel_arg(raw) -> Optional[str]:
    """A worktree-relative path from a request, or None when it is empty,
    absolute or climbs out (``..``)."""
    rel = str(raw or "").strip().replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.strip("/")
    if not rel or "\x00" in rel or os.path.isabs(str(raw or "").strip()):
        return None
    if any(seg == ".." for seg in rel.split("/")):
        return None
    return rel


def _green_preview(inst, wt: str, pattern: str, base: dict) -> dict:
    """The green-only preview fields (see the route)."""
    norm, anchored = _red_zones.normalize_pattern(pattern)
    re_src = base.get("re") or _red_zones.compile_pattern(pattern)
    ci = _red_zone_monitor.root_ci(wt)
    files, _d, _ign, _t = _red_zones.zone_files(
        wt, [{"re": re_src, "pattern": pattern}], cap=_code_map.MAX_FILES, ci=ci
    )
    roots: list = []
    for rel in files:
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):
            pre = "/".join(parts[:i])
            if _red_zones.matches(re_src, pre, ci):
                if pre not in roots:
                    roots.append(pre)
                break
        if len(roots) >= 20:
            break
    repo_id, _repo = _rz_repo(wt)
    doc = _rz_doc(wt, repo_id, ci=ci)
    doc = dict(doc, green=list(doc["green"]) + [{"pattern": pattern, "re": re_src}])
    if len(doc["green"]) == 1:
        doc["companions"] = _red_zones.zones_doc(
            wt,
            repo_id,
            zones=[{"pattern": pattern, "re": re_src, "kind": "green"}],
            ci=ci,
        )["companions"]
    changed, committed = _rz_changed_paths(inst, wt)
    outside = sorted(
        p
        for p in set(changed) | set(committed)
        if _red_zones.classify(p, None, doc, ci) == "outside"
    )
    cset = set(committed)
    body = norm.rstrip("/")
    unanchored = not anchored and "/" not in body
    warnings: list = []
    if not files:
        warnings.append(
            "nothing exists here yet; the agent may only create new files under it"
        )
    anchored_twin = None
    if unanchored:
        if len(roots) == 1:
            anchored_twin = "/" + roots[0]
        warnings.append(
            "`%s` is unanchored: it matches at any depth (%s) — anchor it with a "
            "leading / to scope exactly one folder"
            % (pattern, ", ".join(roots[:5]) or "nowhere yet")
        )
    return {
        "writable_files": len(files),
        "changed_outside": outside[:200],
        "committed_outside": sum(1 for p in outside if p in cset),
        "roots": roots,
        "unanchored": unanchored,
        "anchored": anchored_twin,
        "warnings": warnings,
    }


def _uncommitted_zone_breaches(wt: str) -> list:
    """Zone breaches in what a commit of ``wt`` would take — its uncommitted
    changes, untracked files included — classified by the one predicate
    (``red_zones.breach_verdicts``): ``[{"path", "pattern", "zone_id",
    "kind"}]``, the push gate's shape. ``[]`` with no zones or on git
    failure."""
    ident = _red_zones.repo_identity(wt)
    ci = _red_zone_monitor.root_ci(wt)
    doc = _rz_doc(wt, ident[0] if ident else None, ci=ci)
    if not doc["red"] and not doc["green"]:
        return []
    rels = []
    for entry in _red_zones._git_z(
        wt, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"
    ):
        rel = entry[3:] if len(entry) > 3 else ""
        if rel and rel not in rels:
            rels.append(rel)
    hits = _red_zones.breach_verdicts(doc, rels, ci, root=os.path.realpath(wt))
    return [
        {
            "path": rel,
            "pattern": hits[rel].get("pattern"),
            "zone_id": hits[rel].get("zone_id"),
            "kind": hits[rel].get("kind") or "red",
        }
        for rel in rels
        if rel in hits
    ]


def _zone_breach_message(hits: list) -> str:
    if hits[0].get("kind") == "green":
        msg = "outside green zone: %s" % hits[0]["path"]
    else:
        msg = "red zone changed: %s (%s)" % (hits[0]["path"], hits[0]["pattern"])
    if len(hits) > 1:
        msg += " and %d more" % (len(hits) - 1)
    return msg


def _red_zone_breaches_for(inst, wt: str, target: str = "HEAD") -> list:
    """Zone breaches COMMITTED on the way to ``target`` — computed now, from
    git, never from the monitor's throttled summary (a gate that reads a
    10-second-old answer lets exactly the commit it exists for through).

    The committed range (``fork..HEAD`` for a push, see
    :func:`_pr_committed_changed` for a PR/merge target), classified by the
    ONE predicate (``red_zones.classify``): a path in an enforced red zone
    (repo + worktree scope; a repo zone waived here is excluded), or —
    while a green scope exists — outside it and not a companion, minus the
    exemptions whose blob AT ``target`` still equals the recorded one (work
    committed before the scope existed; editing it again re-arms the gate)
    → ``[{"path", "pattern", "zone_id", "kind"}]``, matched
    case-insensitively where the filesystem is (as the hook does). A git
    failure is ``[]``: the gate fails open like every other probe in the push
    path."""
    ident = _red_zones.repo_identity(wt)
    ci = _red_zone_monitor.root_ci(wt)
    doc = _rz_doc(wt, ident[0] if ident else None, ci=ci)
    if not doc["red"] and not doc["green"]:
        return []
    if target == "HEAD":
        rels = _code_map.committed_changed(inst, wt, "HEAD")
    else:
        rels = _pr_committed_changed(inst, wt, target)
    rev = _code_map._rev(target) or target
    hits = _red_zones.breach_verdicts(doc, rels, ci, root=os.path.realpath(wt), rev=rev)
    return [
        {
            "path": rel,
            "pattern": hits[rel].get("pattern"),
            "zone_id": hits[rel].get("zone_id"),
            "kind": hits[rel].get("kind") or "red",
        }
        for rel in rels
        if rel in hits
    ]


def _pr_committed_changed(inst, wt: str, target: str) -> list:
    """What a PR/merge of ``target`` (``origin/<branch>``) would carry, as the
    FORGE sees it: ``<base>...<target>`` — the diff from the merge-base of the
    base branch and the target to the target (``refs/remotes/origin/<base>``
    first, then the local ``refs/heads/<base>``; full refs, so a tag or branch
    named like one can't stand in for it).

    WHY NOT ``fork..target``. The session's fork point is the merge-base of
    the LOCAL ``HEAD`` with the base. Merge (or rebase onto) a newer main
    locally without pushing and the fork jumps to the new main tip, while the
    pushed branch still sits on the old one — so a teammate's legitimate
    change to a zoned file on main shows up, reversed, as this branch's
    breach, and Make PR / Merge / the autopilot halt on a change the PR
    doesn't contain. The same happens the other way round after GitHub's
    "Update branch" merged main into the remote branch. Falls back to
    ``committed_changed`` when no base ref resolves. ``[]`` on git failure."""
    srv = _server()
    try:
        tgt = _code_map._rev(target)
        base = str(srv._session_base_branch(inst) or "").strip()
        if tgt and base and not base.startswith("-"):
            for ref in ("refs/remotes/origin/" + base, "refs/heads/" + base):
                if not srv._git_ref_exists(wt, ref):
                    continue
                out = _code_map._git(
                    wt,
                    "diff",
                    "--name-only",
                    "-z",
                    "--no-renames",
                    ref + "..." + tgt,
                    "--",
                )
                return sorted(set(_code_map._z_fields(out))) if out is not None else []
        return _code_map.committed_changed(inst, wt, target)
    except Exception:  # noqa: BLE001 — the gate fails open on its own bugs
        return []


def _pr_content_ref(wt: str, branch: str) -> str:
    """What a PR/merge would actually carry: ``origin/<branch>`` when that ref
    exists (the pushed branch IS the PR), else the local ``HEAD``."""
    if branch:
        cp = _server()._run_capped(
            [
                "git",
                "-C",
                wt,
                "rev-parse",
                "--verify",
                "--quiet",
                "refs/remotes/origin/" + branch,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if cp.returncode == 0:
            return "origin/" + branch
    return "HEAD"
