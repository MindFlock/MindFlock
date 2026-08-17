"""Per-session sidebar descriptors: names, folder labels, diff stats, JSON.

Everything that turns a :class:`backend.session.Instance` into the dict the
UI polls every ~4s: the repo/folder labels shown in the sidebar, the J3
one-line diff stat (total change vs the session's fork point, cached ~10s per
worktree), and :func:`_instance_json`, the per-session payload assembled by
``_build_instances_snapshot`` in the server.

Split out of ``backend.web.server`` (which re-imports these names — the
routes, the tick loops, and tests reference them through the server
namespace; ``core.agent_state`` pops ``_DIFF_STAT_CACHE`` entries on branch
drift via that same alias).
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from typing import Dict, Optional

from backend import config, providers, session
from backend.session import provisioned as provisioning
from backend.session import tmux


def _server():
    """The ``backend.web.server`` module, imported lazily (it imports this
    module at startup, so a top-level import would be circular)."""
    from backend.web import server

    return server


def _repo_name(inst: session.Instance) -> str:
    """Human name of the repo a session is based on (shown in the sidebar).

    For provisioned sessions on the configured repo that's the configured
    MindFlock repo; for plain sessions it's the basename of the chosen repo the
    worktree was cut from.
    """
    if getattr(inst, "Provisioned", False):
        # K2: a provisioned-on-local-repo session knows its target repo from
        # creation (provision_repo / an adopted workspace path) — prefer it, so
        # the sidebar doesn't report the CONFIGURED repo's name while Start is
        # still running (worktree not yet resolvable).
        local = getattr(inst, "_provision_repo", "") or getattr(
            inst, "_workspace_path", ""
        )
        if local:
            return os.path.basename(os.path.normpath(local))
        try:
            repo_path = ""
            try:
                repo_path = inst.GetGitWorktree().GetRepoPath()
            except Exception:  # noqa: BLE001
                repo_path = ""
            s = provisioning.settings_for_workspace(repo_path)
            url = getattr(s, "repo_url", "") if s else ""
            if url:
                # The repo COMPONENT of the remote, not its "/"-tail: an
                # scp-style remote with a one-segment path
                # (``git@github.com:app.git``) contains no "/" at all, so the
                # tail split labelled the sidebar ``git@github.com:app``. Local
                # clone sources have no forge and still use the tail.
                base = provisioning.repo_display_name(url)
                if base:
                    return base
        except Exception:  # noqa: BLE001
            pass
    repo_path = ""
    try:
        repo_path = inst.GetGitWorktree().GetRepoPath()
    except Exception:  # noqa: BLE001
        repo_path = inst.Path or ""
    if not repo_path or repo_path == ".":
        return ""
    return os.path.basename(os.path.normpath(repo_path))


def _folder_label(folder: str) -> str:
    """Short label for a session's folder shown in the sidebar.

    For a worktree (under ``<configDir>/worktrees``) show the leaf's PARENT dir
    (e.g. ``.../worktrees/mindflock/gamer3_<hex>`` -> ``mindflock``); otherwise
    show the current directory's name. The full path stays in the tooltip/copy.
    """
    if not folder:
        return ""
    real = os.path.realpath(folder)
    try:
        wt_root = os.path.realpath(os.path.join(config.GetConfigDir(), "worktrees"))
    except Exception:  # noqa: BLE001
        wt_root = ""
    if wt_root and (real == wt_root or real.startswith(wt_root + os.sep)):
        parent = os.path.basename(os.path.dirname(real))
        if parent and parent != "worktrees":
            return parent
        # Worktree sits directly under worktrees/ — fall back to the leaf with
        # its ``_<hex>`` suffix stripped.
        return re.sub(r"_[0-9a-f]{6,}$", "", os.path.basename(real))
    return os.path.basename(real)


# J3: per-session one-line diff stat for the sidebar/pane context line.
# Cached ~10s per worktree so the UI's 4s poll stays cheap. Each entry also
# carries the worktree-state fingerprint at compute time: when the TTL lapses
# but the fingerprint still matches, the cached numbers are re-armed instead
# of re-running the shortstat pair (which re-counts every changed line —
# seconds of CPU on a big session diff, otherwise burned every 10s forever).
_DIFF_STAT_CACHE: Dict[str, tuple] = (
    {}
)  # worktree -> (expires_epoch, dict|None, fingerprint|None)
_DIFF_STAT_TTL = 10.0
# Fingerprinting stats every dirty/untracked path; past this many, skip it and
# fall back to plain TTL caching (a pathological status listing would cost
# more to stat than it saves).
_FP_MAX_PATHS = 5000


def _worktree_fingerprint(wt: str, base_ref: str) -> Optional[str]:
    """Cheap, content-sensitive fingerprint of everything the session diff
    stat depends on: HEAD, the fork point, the dirty/untracked path set, and
    each such path's (mtime_ns, size) — ``git status`` says WHICH paths
    differ, not what's in them, so without the stat pass an edit that keeps
    the path list identical would go unnoticed. ~10ms where the shortstat
    pair costs seconds. ``None`` = can't fingerprint (git trouble / too many
    paths) — the caller then falls back to plain TTL caching."""
    try:
        head = subprocess.run(
            ["git", "-C", wt, "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        if head.returncode != 0:
            return None
        st = subprocess.run(
            ["git", "-C", wt, "status", "--porcelain", "-z", "--untracked-files=all"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        if st.returncode != 0:
            return None
        # Porcelain v1 -z records: "XY path" NUL, with rename/copy entries
        # followed by the source path as its own NUL-separated field.
        paths = []
        fields = st.stdout.split(b"\0")
        i = 0
        while i < len(fields):
            rec = fields[i]
            i += 1
            if len(rec) < 4:
                continue
            paths.append(rec[3:])
            if rec[:1] in (b"R", b"C") and i < len(fields):
                paths.append(fields[i])
                i += 1
        if len(paths) > _FP_MAX_PATHS:
            return None
        h = hashlib.sha1()
        h.update(head.stdout)
        h.update(base_ref.encode("utf-8", "replace"))
        h.update(st.stdout)
        for p in paths:
            try:
                s = os.stat(os.path.join(wt, os.fsdecode(p)))
                h.update(b"%d:%d;" % (s.st_mtime_ns, s.st_size))
            except OSError:
                h.update(b"gone;")
        return h.hexdigest()
    except Exception:  # noqa: BLE001
        return None


def _session_fork_point(inst, wt: str) -> str:
    """The commit the session's work is measured against: the merge-base of
    HEAD and the K1-resolved per-session base branch (``origin/<base>`` first,
    then local ``<base>``), falling back to the worktree's recorded base
    commit, then ``HEAD``. Shared by the header diff stat and the Diff tab so
    the two always agree on the baseline.
    """
    base = _server()._session_base_branch(inst)
    for ref in ("origin/" + base, base):
        cp = subprocess.run(
            ["git", "-C", wt, "merge-base", "HEAD", ref],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        if cp.returncode == 0:
            sha = cp.stdout.decode("utf-8", "replace").strip()
            if sha:
                return sha
    try:
        return inst.GetGitWorktree().GetBaseCommitSHA() or "HEAD"
    except Exception:  # noqa: BLE001
        return "HEAD"


def _parse_shortstat(out: str) -> dict:
    # " 3 files changed, 42 insertions(+), 7 deletions(-)" (any part may be
    # missing); empty output = no changes.
    result = {"files": 0, "additions": 0, "deletions": 0}
    m = re.search(r"(\d+)\s+files?\s+changed", out)
    if m:
        result["files"] = int(m.group(1))
    m = re.search(r"(\d+)\s+insertions?\(\+\)", out)
    if m:
        result["additions"] = int(m.group(1))
    m = re.search(r"(\d+)\s+deletions?\(-\)", out)
    if m:
        result["deletions"] = int(m.group(1))
    return result


def _session_diff_stat(inst) -> Optional[dict]:
    """``{"files", "additions", "deletions", "uncommitted": {…}}`` or ``None``.

    The single most useful number for triage: the TOTAL change the session has
    produced — committed-beyond-base PLUS working-tree changes — i.e. one
    ``git diff --shortstat <fork-point>`` (see :func:`_session_fork_point`).
    Untracked files count too: they're staged as intent-to-add first (the same
    ``git add -N .`` the Diff tab has always run, so this adds no new kind of
    index mutation). ``uncommitted`` carries the not-yet-committed slice
    (``git diff --shortstat HEAD``) so the UI can show both totals.

    ``None`` when unavailable: paused / loading / no worktree / git failure.
    """
    srv = _server()
    try:
        if not inst.Started() or inst.Status == session.Paused:
            return None
        wt = inst.GetWorktreePath()
        if not wt or not os.path.isdir(wt):
            return None
        now = time.time()
        cached = _DIFF_STAT_CACHE.get(wt)
        if cached and cached[0] > now:
            return cached[1]
        base_ref = srv._session_fork_point(inst, wt)
        fp = _worktree_fingerprint(wt, base_ref)
        if cached and fp is not None and len(cached) > 2 and cached[2] == fp:
            # TTL lapsed but HEAD/fork-point/dirty-set are exactly as at the
            # last full compute — the numbers cannot have moved. Re-arm the
            # TTL and skip the expensive shortstat pair.
            _DIFF_STAT_CACHE[wt] = (now + _DIFF_STAT_TTL, cached[1], fp)
            return cached[1]
        subprocess.run(
            ["git", "-C", wt, "add", "-N", "."],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        cp = subprocess.run(
            ["git", "-C", wt, "diff", "--shortstat", base_ref],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        if cp.returncode != 0:
            _DIFF_STAT_CACHE[wt] = (now + _DIFF_STAT_TTL, None, None)
            return None
        result = _parse_shortstat(cp.stdout.decode("utf-8", "replace").strip())
        cp = subprocess.run(
            ["git", "-C", wt, "diff", "--shortstat", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        un = (
            _parse_shortstat(cp.stdout.decode("utf-8", "replace").strip())
            if cp.returncode == 0
            else {"additions": 0, "deletions": 0}
        )
        result["uncommitted"] = {
            "additions": un["additions"],
            "deletions": un["deletions"],
        }
        # Fingerprint AFTER `add -N`: intent-to-add entries change the status
        # output, so the stored state must match what the next probe sees.
        _DIFF_STAT_CACHE[wt] = (
            now + _DIFF_STAT_TTL,
            result,
            _worktree_fingerprint(wt, base_ref),
        )
        return result
    except Exception:  # noqa: BLE001
        return None


def _instance_json(inst: session.Instance, cheap: bool = False) -> dict:
    """The per-session sidebar dict. ``cheap=True`` skips the fields that
    shell out to git (diff stat, origin probe) — they come back as None/False
    placeholders — so the fast first-paint path never blocks on a subprocess;
    every other field is computed exactly as in the full build."""
    srv = _server()
    status_name = getattr(inst.Status, "name", str(inst.Status)).lower()
    # Working folder for this session (worktree dir once started, else the base
    # path). Shown + copyable in the sidebar's expanded panel.
    try:
        folder = inst.GetWorktreePath() if inst.Started() else (inst.Path or "")
    except Exception:  # noqa: BLE001
        folder = inst.Path or ""
    # L1(c): a started session whose workspace directory vanished (deleted
    # outside MindFlock) — surfaced distinctly so the UI can offer "Clean up"
    # instead of a fake running terminal. Paused sessions have no dir by design.
    try:
        workspace_missing = bool(
            inst.Started()
            and inst.Status != session.Paused
            and folder
            and not os.path.isdir(folder)
        )
    except Exception:  # noqa: BLE001
        workspace_missing = False
    # Which auth profile the session's agent runs under: the stored pin plus
    # its resolution ("" pin -> the global default), so the UI can show the
    # active identity without re-implementing the tri-state.
    profile_id = getattr(inst, "ProfileId", "") or ""
    profile_effective = ""
    profile_label = ""
    try:
        from backend.providers import auth_profiles

        profile_effective = auth_profiles.effective_profile_id(profile_id)
        prof = auth_profiles.get_profile(profile_effective)
        if prof is not None:
            profile_label = prof.display_label()
        else:
            profile_effective = ""
    except Exception:  # noqa: BLE001 — profiles are enrichment only
        profile_effective = ""
    return {
        "title": inst.Title,
        "branch": inst.Branch,
        "repo": srv._repo_name(inst),
        "folder": folder,
        "folder_label": srv._folder_label(folder),
        "program": inst.Program,
        # The resolved coding provider for this session (claude / codex / …) —
        # the canonical identity behind the raw Program string, used by the UI
        # to label each window's usage with exactly who is serving it.
        "provider": providers.resolve(getattr(inst, "Program", "") or "").name,
        "profile_id": profile_id,
        "profile_effective": profile_effective,
        "profile_label": profile_label,
        "path": inst.Path,
        "status": status_name,
        "started": inst.Started(),
        "tmux_name": tmux.to_mindflock_tmux_name(inst.Title),
        "provisioned": getattr(inst, "Provisioned", False),
        "workspace_strategy": getattr(inst, "WorkspaceStrategy", "worktree"),
        "in_place": getattr(inst, "InPlace", False),
        # J3: {"files", "additions", "deletions"} — total change the session
        # has produced vs its per-session base — or null when unavailable.
        "diff_stat": None if cheap else srv._session_diff_stat(inst),
        # L1(c): true when the session's workspace dir no longer exists.
        "workspace_missing": workspace_missing,
        # L2: whether the workspace has an `origin` remote (cached ~30s) — the
        # guided Push step is a dead end without one.
        "has_origin": bool(
            not cheap and not workspace_missing and srv._has_origin(folder)
        ),
    }
