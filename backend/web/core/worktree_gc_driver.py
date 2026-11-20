"""The worktree garbage collector's server side: who uses what, the cached
report behind Settings → Disk cleanup, the background pass, the setting and
its one-time notice. The git half — inventory, classifier, removal — is
:mod:`backend.web.core.worktree_gc`.

**Who uses what** (:func:`build_usage`) — an item is in use when any of
these names it, and then nothing removes it, not even on request:

* a session of THIS server (``ENGINE.instances``: running, ready, starting,
  paused — sleeping is a session too), or of ANY server sharing the state
  file (``state.json``, which the second server and the ticket pipeline
  write);
* a session closed within the age line (it can still be reopened from
  Recently closed: the closed record keeps its folder);
* a route that is not finished (``flights.list_flights``), a team run task
  that is not finished (``team_runs.list_runs``), queued prompts
  (``prompt_queue``) or a queued launch (``launch_queue.json``) — by session
  title, and by repo + branch where they carry one.

Built from the files alone when ``srv`` is None (``mindflock clean`` with no
server running), so the CLI and the server can never disagree about what is
in use.

**The report** (:func:`report`) is computed off the event loop
(``srv._in_bg_pool`` / a thread) and cached; a GET returns the last one and
starts a fresh scan when it is older than :data:`STALE_AFTER_S` (or on
``refresh``). Sizes come from ``du`` and are cached per folder.

**The pass** (:func:`run_loop`): :data:`FIRST_DELAY_S` after boot, then every
:data:`INTERVAL_S`. ``cleanup.mode`` decides: ``auto`` collects the safe set
(at most :data:`worktree_gc.MAX_PER_PASS` per pass); ``ask`` (the default)
scans and, the first time the safe set is worth reclaiming (≥
:data:`NOTICE_BYTES` or ≥ :data:`NOTICE_COUNT` items), emits ONE
``system.cleanup_available`` (the bell + phone rule ``cleanup_available``);
it says so again only after the person has collected something and a new
pile has built up. ``off`` does nothing. Off under pytest and with
``MINDFLOCK_WORKTREE_GC=0``. Never raises.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import threading
import time
from typing import Dict, List, Optional

from backend.web.core import worktree_gc as gc

#: The first pass, after boot (past the boot quiet window and the launches).
FIRST_DELAY_S = 15 * 60.0
#: Between passes.
INTERVAL_S = 6 * 3600.0
#: A cached report older than this is refreshed on the next GET.
STALE_AFTER_S = 10 * 60.0
#: "A meaningful amount to reclaim": either of these.
NOTICE_BYTES = 2 * 1024**3
NOTICE_COUNT = 10
#: PR lookups (network) per scan at most — the last rung, rarely needed.
MAX_PR_CHECKS = 15

#: Test seam: None = on except under pytest / MINDFLOCK_WORKTREE_GC=0.
ENABLED: Optional[bool] = None

STATE_FILE = "worktree_gc.json"

_STATUS = {0: "running", 1: "ready", 2: "starting", 3: "paused"}

_lock = threading.Lock()
_remove_lock = threading.Lock()
_report: dict = {}
_scanning = threading.Event()


def enabled() -> bool:
    if ENABLED is not None:
        return bool(ENABLED)
    if os.environ.get("MINDFLOCK_WORKTREE_GC", "1") == "0":
        return False
    from backend.web.core import restart as _restart

    return not _restart._under_pytest()


# ------------------------------------------------------------------ settings


def settings() -> dict:
    """``{"mode": "auto"|"ask"|"off", "after_days": n}`` (blank mode = ask,
    no days = 14). Never raises."""
    mode, days = "", None
    try:
        # Local import, like every settings reader that runs in the server.
        from backend.config.settings import load_settings

        c = load_settings().cleanup
        mode, days = str(c.mode or ""), c.after_days
    except Exception:  # noqa: BLE001
        pass
    if mode not in ("auto", "ask", "off"):
        mode = "ask"
    return {"mode": mode, "after_days": int(days or gc.DEFAULT_AFTER_DAYS)}


def set_settings(mode: Optional[str] = None, after_days: Optional[int] = None) -> dict:
    """Save ``cleanup.mode`` / ``cleanup.after_days``; raises ValueError on a
    bad value, ``SettingsUnreadable`` when settings.json can't be read."""
    from backend.config.settings import CLEANUP_MODES, update_settings

    patch: dict = {}
    if mode is not None:
        m = str(mode).strip().lower()
        if m not in CLEANUP_MODES:
            raise ValueError("mode must be one of auto, ask, off")
        patch["mode"] = m
    if after_days is not None:
        try:
            d = int(after_days)
        except (TypeError, ValueError):
            raise ValueError("after_days must be a whole number of days")
        if d < 1 or d > 3650:
            raise ValueError("after_days must be between 1 and 3650")
        patch["after_days"] = d
    if patch:
        update_settings(cleanup=patch)
    return settings()


# ------------------------------------------------------------- who uses what


def _iso(value) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _disk_instances() -> List[dict]:
    """state.json's sessions (every server's), as dicts. [] on any trouble."""
    try:
        from backend import config

        raw = config.LoadState().GetInstances()
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8")
        items = json.loads(raw) if raw else []
        return [x for x in items if isinstance(x, dict)]
    except Exception:  # noqa: BLE001
        return []


def _sleeping() -> set:
    try:
        from backend.web.core import sleep as _sleep

        return set(_sleep._load().keys())
    except Exception:  # noqa: BLE001
        return set()


def _closed_entries(srv) -> List[dict]:
    try:
        if srv is not None and hasattr(srv, "_load_recently_closed"):
            return list(srv._load_recently_closed())
        from backend.web.core.recently_closed import _load_recently_closed

        return list(_load_recently_closed())
    except Exception:  # noqa: BLE001
        return []


def _clone_roots() -> List[str]:
    try:
        from backend.web.core.workspaces import _workspace_roots

        wt = gc.norm(gc.worktrees_root())
        return [r for r in _workspace_roots() if gc.norm(r) != wt]
    except Exception:  # noqa: BLE001
        return []


def build_usage(
    srv=None, *, after_days: Optional[float] = None, now: Optional[float] = None
) -> gc.Usage:
    """Everything that keeps an item (see the module docstring). ``srv`` =
    the server module (its live engine and PR lookup), None = files only."""
    now = time.time() if now is None else now
    days = float(after_days if after_days is not None else settings()["after_days"])
    u = gc.Usage(clone_roots=_clone_roots())
    title_path: Dict[str, str] = {}
    sleeping = _sleeping()

    def note(
        title: str,
        path: str,
        repo: str,
        branch: str,
        base: str,
        existing: bool,
        why: str,
        ts: float,
    ) -> None:
        if not path:
            return
        title_path.setdefault(title, path)
        u.use(path, why)
        u.touched(path, ts)
        if repo:
            u.repos.add(gc.norm(repo))
        u.branch_info.setdefault(
            gc.norm(path),
            {
                "title": title,
                "branch": branch,
                "base": base,
                "existing_branch": existing,
            },
        )

    if srv is not None:
        engine = getattr(srv, "ENGINE", None)
        for title, inst in list(getattr(engine, "instances", {}).items()):
            try:
                path = inst.GetWorktreePath() or ""
            except Exception:  # noqa: BLE001
                path = ""
            try:
                wt = inst.GetGitWorktree()
                repo = wt.GetRepoPath() or ""
                branch = wt.GetBranchName() or ""
                existing = bool(wt.IsExistingBranch())
            except Exception:  # noqa: BLE001
                repo, branch, existing = "", getattr(inst, "Branch", "") or "", False
            status = (
                "sleeping"
                if title in sleeping
                else _STATUS.get(int(getattr(inst, "Status", 0) or 0), "open")
            )
            upd = getattr(inst, "UpdatedAt", None)
            ts = upd.timestamp() if hasattr(upd, "timestamp") else 0.0
            note(
                title,
                path,
                repo,
                branch,
                getattr(inst, "base_branch", "") or "",
                existing,
                "session '%s' (%s)" % (title, status),
                ts,
            )
    for d in _disk_instances():
        title = str(d.get("title") or "")
        wt = d.get("worktree") if isinstance(d.get("worktree"), dict) else {}
        path = str(wt.get("worktree_path") or "") or (
            str(d.get("path") or "") if d.get("in_place") else ""
        )
        status = (
            "sleeping"
            if title in sleeping
            else _STATUS.get(int(d.get("status") or 0), "open")
        )
        note(
            title,
            path,
            str(wt.get("repo_path") or ""),
            str(wt.get("branch_name") or d.get("branch") or ""),
            str(d.get("base_branch") or ""),
            bool(wt.get("is_existing_branch")),
            "session '%s' (%s)" % (title, status),
            _iso(d.get("updated_at")),
        )

    for e in _closed_entries(srv):
        folder = str(e.get("folder") or "")
        if not folder or e.get("wiped"):
            continue
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        wt = data.get("worktree") if isinstance(data.get("worktree"), dict) else {}
        closed = _iso(e.get("closed_at"))
        title = str(e.get("title") or "")
        u.touched(folder, closed)
        repo = str(wt.get("repo_path") or e.get("repo") or "")
        if repo:
            u.repos.add(gc.norm(repo))
        u.branch_info.setdefault(
            gc.norm(folder),
            {
                "title": title,
                "branch": str(wt.get("branch_name") or e.get("branch") or ""),
                "base": str(data.get("base_branch") or ""),
                "existing_branch": bool(wt.get("is_existing_branch")),
            },
        )
        title_path.setdefault(title, folder)
        if closed and now - closed < days * gc.DAY:
            u.use(
                folder,
                "session '%s' closed %s; it can still be reopened"
                % (title, gc._ago(now - closed)),
            )

    refs = []  # (why, title, repo_root, branch)
    try:
        from backend.web.core import flights as _flights

        for f in _flights.list_flights(include_finished=False):
            why = "route '%s' (%s)" % (
                f.get("plan_name") or f.get("id"),
                f.get("state"),
            )
            refs.append(
                (
                    why,
                    f.get("session") or "",
                    f.get("repo_root") or "",
                    f.get("branch") or "",
                )
            )
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.web.core import team_runs as _runs

        for run in _runs.list_runs(include_finished=False):
            for t in run.get("tasks") or []:
                if t.get("state") in _runs.TERMINAL:
                    continue
                why = "team run '%s' (%s)" % (
                    run.get("name") or run.get("id"),
                    t.get("state"),
                )
                refs.append(
                    (
                        why,
                        t.get("title") or "",
                        t.get("repo_root") or "",
                        t.get("branch") or "",
                    )
                )
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.web.core import prompt_queue as _pq

        for title in _pq.all_titles():
            refs.append(("queued prompts for '%s'" % title, title, "", ""))
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.web.core import launch_gate as _lg

        q = _read_json(_lg.queue_path())
        items = q.get("items") if isinstance(q, dict) else q
        for it in items or []:
            if isinstance(it, dict) and it.get("title"):
                refs.append(
                    ("a queued launch of '%s'" % it["title"], it["title"], "", "")
                )
    except Exception:  # noqa: BLE001
        pass
    for why, title, repo_root, branch in refs:
        if title and title in title_path:
            u.use(title_path[title], why)
        if repo_root and branch and os.path.isdir(repo_root):
            from backend import gitutil

            held = gitutil.worktree_for_branch(
                repo_root, branch, timeout=gc.GIT_TIMEOUT
            )
            if held:
                u.use(held, why)

    if srv is not None and hasattr(srv, "_pr_info"):
        budget = [MAX_PR_CHECKS]

        def pr_merged(item: dict) -> bool:
            if budget[0] <= 0:
                return False
            budget[0] -= 1
            info = srv._pr_info(item["path"], item["branch"])
            if (
                not isinstance(info, dict)
                or str(info.get("state") or "").upper() != "MERGED"
            ):
                return False
            head = str(info.get("head") or info.get("headRefOid") or "")
            # Merged at THIS head, or it is not proof: a commit made after the
            # PR merged is work the PR never carried.
            return bool(head) and head == item.get("head")

        u.pr_merged = pr_merged
    return u


# --------------------------------------------------------------- the report


def _state_path() -> str:
    return os.path.join(gc.config_dir(), STATE_FILE)


def _state() -> dict:
    d = _read_json(_state_path())
    return d if isinstance(d, dict) else {}


def _save_state(d: dict) -> None:
    try:
        p = _state_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, p)
    except OSError:
        pass


def scan(srv=None, *, measure: bool = True, now: Optional[float] = None) -> dict:
    """A fresh report (blocking: git + du). Cached for :func:`report`."""
    now = time.time() if now is None else now
    cfg = settings()
    _scanning.set()
    try:
        usage = build_usage(srv, after_days=cfg["after_days"], now=now)
        items = gc.inventory(
            usage, after_days=cfg["after_days"], now=now, measure=measure
        )
        rep = {
            "items": items,
            # One du per headline number: a file shared by many checkouts
            # (hard-linked caches) or an item inside another counts once.
            "summary": gc.summarize(
                items, gc.measure_totals(items) if measure else None
            ),
            "scanned_at": now,
            "settings": cfg,
            "worktrees_root": gc.worktrees_root(),
            "kept_dir": gc.kept_root(),
            "last_collect": _state().get("last_collect"),
        }
        with _lock:
            _report.clear()
            _report.update(rep)
        return rep
    finally:
        _scanning.clear()


def _scan_in_background(srv) -> None:
    if _scanning.is_set():
        return
    _scanning.set()  # claimed before the thread starts, so two GETs start one

    def run():
        try:
            scan(srv)
        except Exception as exc:  # noqa: BLE001
            with _lock:
                _report["error"] = str(exc)
        finally:
            _scanning.clear()

    threading.Thread(target=run, name="worktree-gc-scan", daemon=True).start()


def report(srv=None, *, refresh: bool = False) -> dict:
    """The last report (``items`` may be absent before the first scan), with
    ``scanning`` true while a fresh one is being made. Starts one when the
    last is older than :data:`STALE_AFTER_S` or ``refresh``. Never blocks."""
    with _lock:
        rep = dict(_report)
    age = time.time() - float(rep.get("scanned_at") or 0)
    if refresh or not rep or age > STALE_AFTER_S:
        _scan_in_background(srv)
    rep["scanning"] = _scanning.is_set()
    rep["settings"] = settings()
    rep.setdefault("items", None)
    return rep


# ---------------------------------------------------------------- removing


def _after_remove(srv):
    """What else a removed folder leaves: its Claude trust entry, an editor
    window on it, and recently-closed rows that would offer a Reopen that can
    only fail."""

    def after(item: dict) -> None:
        path = item["path"]
        try:
            from backend.providers.claude import remove_trust_entry

            remove_trust_entry(path)
        except Exception:  # noqa: BLE001
            pass
        try:
            from backend.web.core.cursor_windows import _close_cursor_window

            _close_cursor_window(path)
        except Exception:  # noqa: BLE001
            pass
        try:
            from backend.web.core import recently_closed as rc

            load = (
                getattr(srv, "_load_recently_closed", None) or rc._load_recently_closed
            )
            save = (
                getattr(srv, "_save_recently_closed", None) or rc._save_recently_closed
            )
            entries = load()
            keep = [
                e
                for e in entries
                if gc.norm(str(e.get("folder") or "")) != gc.norm(path)
            ]
            if len(keep) != len(entries):
                save(keep)
        except Exception:  # noqa: BLE001
            pass

    return after


def _tidy() -> int:
    """Remove the empty branch-slug folders removals leave under the root."""
    try:
        from backend.web.core.workspaces import _prune_empty_worktree_dirs

        return _prune_empty_worktree_dirs()
    except Exception:  # noqa: BLE001
        return 0


def _refresh_one(item: dict, usage: gc.Usage, after_days: float) -> dict:
    """``item`` classified again from scratch (its git record re-read), for
    the moment right before a removal. Raises LookupError when it is gone."""
    path = item["path"]
    rec = None
    if item.get("kind") == gc.KIND_WORKTREE and item.get("repo"):
        from backend import gitutil

        for i, wt in enumerate(
            gitutil.worktree_list(item["repo"], timeout=gc.GIT_TIMEOUT) or ()
        ):
            if i and gc.norm(str(wt.get("path") or "")) == path:
                branch = str(wt.get("branch") or "")
                rec = gc._record(
                    path,
                    gc.KIND_WORKTREE,
                    item["repo"],
                    branch=(
                        branch[len("refs/heads/") :]
                        if branch.startswith("refs/heads/")
                        else branch
                    ),
                    head=str(wt.get("head") or ""),
                    detached=bool(wt.get("detached")),
                    locked=bool(wt.get("locked")),
                    prunable=bool(wt.get("prunable")) or not os.path.isdir(path),
                )
                break
        if rec is None:
            raise LookupError("git no longer lists it")
    else:
        rec = gc._record(
            path, item.get("kind") or gc.KIND_ORPHAN, item.get("repo") or ""
        )
    fresh = gc.classify(rec, usage, after_days=after_days, measure=False)
    for k in ("size_bytes", "free_bytes"):
        if fresh.get(k) is None:
            fresh[k] = item.get(k)
    return fresh


def collect_now(srv=None, *, limit: Optional[int] = None, log=None) -> dict:
    """Scan, then remove every safe item (each re-checked right before).
    Blocking. Returns :func:`worktree_gc.collect`'s result plus ``tidied``."""
    with _remove_lock:
        cfg = settings()
        rep = scan(srv)
        usage = build_usage(srv, after_days=cfg["after_days"])
        res = gc.collect(
            rep["items"],
            lambda it: _refresh_one(
                it, build_usage(srv, after_days=cfg["after_days"]), cfg["after_days"]
            ),
            usage=usage,
            limit=limit if limit is not None else 10**6,
            after_remove=_after_remove(srv),
        )
        res["tidied"] = _tidy() if res["removed"] else 0
        st = _state()
        st["last_collect"] = {
            "at": time.time(),
            "removed": len(res["removed"]),
            "freed_bytes": res["freed_bytes"],
        }
        if res["removed"]:
            st["notified"] = False  # a new pile may be told about again
        _save_state(st)
        if log is not None and res["removed"]:
            log(
                "worktree cleanup: removed %d (%s), deleted %d merged branches"
                % (
                    len(res["removed"]),
                    _human(res["freed_bytes"]),
                    len(res["branches_deleted"]),
                )
            )
        _drop_from_report([r["path"] for r in res["removed"]])
        return res


def remove_item(srv, item_id: str, *, confirm: bool) -> dict:
    """Remove the item ``item_id`` of the last report on a person's request
    (see :func:`worktree_gc.remove_one`). 404-ish ``{"ok": False,
    "missing": True}`` when the id isn't in the last report."""
    with _lock:
        items = list(_report.get("items") or [])
    item = next((i for i in items if i.get("id") == item_id), None)
    if item is None:
        return {
            "ok": False,
            "missing": True,
            "error": "Not in the current list; refresh it.",
        }
    with _remove_lock:
        cfg = settings()
        usage = build_usage(srv, after_days=cfg["after_days"])
        try:
            fresh = _refresh_one(item, usage, cfg["after_days"])
        except LookupError as exc:
            _drop_from_report([item["path"]])
            return {
                "ok": False,
                "missing": True,
                "error": "It is already gone (%s)." % exc,
            }
        res = gc.remove_one(
            fresh, confirmed=confirm, usage=usage, after_remove=_after_remove(srv)
        )
        if res.get("ok"):
            _tidy()
            _drop_from_report([fresh["path"]])
            st = _state()
            st["notified"] = False
            _save_state(st)
        res["item"] = {
            k: fresh.get(k)
            for k in ("id", "path", "name", "kind", "branch", "kept", "safe", "state")
        }
        return res


def _drop_from_report(paths) -> None:
    gone = {gc.norm(p) for p in paths}
    if not gone:
        return
    with _lock:
        items = _report.get("items")
        if isinstance(items, list):
            removed = [i for i in items if gc.norm(i.get("path") or "") in gone]
            _report["items"] = [
                i for i in items if gc.norm(i.get("path") or "") not in gone
            ]
            # Keep the measured (deduplicated) totals, less what went, until
            # the next scan measures again: a plain per-item sum would bring
            # the over-count back.
            old = _report.get("summary") or {}
            new = gc.summarize(_report["items"])
            freed = sum(int(i.get("size_bytes") or 0) for i in removed)
            freed_safe = sum(
                int(i.get("free_bytes") or i.get("size_bytes") or 0)
                for i in removed
                if i.get("safe")
            )
            for k, less in (("bytes", freed), ("safe_bytes", freed_safe)):
                if k in old:
                    new[k] = max(0, int(old[k]) - less)
            if "other_bytes" in old:
                new["other_bytes"] = old["other_bytes"]
            if not new["safe_count"]:
                new["safe_bytes"] = 0
            _report["summary"] = new


def _human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "%d B" % n


def notice_text(count: int, nbytes: int) -> str:
    return (
        "%d leftover worktree%s (%s) can be removed safely: merged or pushed, "
        "nothing uncommitted, unused for a while. Settings → Disk cleanup."
        % (count, "" if count == 1 else "s", _human(nbytes))
    )


def maybe_notify(srv, rep: dict) -> bool:
    """``ask`` mode: emit ONE ``system.cleanup_available`` the first time the
    safe set is worth reclaiming; again only after a collection and a new
    pile. Returns whether it emitted."""
    s = rep.get("summary") or {}
    count, nbytes = int(s.get("safe_count") or 0), int(s.get("safe_bytes") or 0)
    if count == 0 or (nbytes < NOTICE_BYTES and count < NOTICE_COUNT):
        return False
    st = _state()
    if st.get("notified"):
        return False
    try:
        from backend.web.core import events as _events

        _events.BUS.emit(
            "system.cleanup_available",
            new=str(count),
            data={
                "count": count,
                "bytes": nbytes,
                "detail": notice_text(count, nbytes),
            },
        )
    except Exception:  # noqa: BLE001
        return False
    st["notified"] = True
    st["notified_at"] = time.time()
    _save_state(st)
    return True


def pass_once(srv) -> dict:
    """One background pass per :func:`settings`. Never raises."""
    try:
        cfg = settings()
        if cfg["mode"] == "off":
            return {"mode": "off"}
        log = getattr(getattr(srv, "log", None), "InfoLog", None)
        printf = (lambda m: log.Printf("%s", m)) if log is not None else None
        if cfg["mode"] == "auto":
            res = collect_now(srv, limit=gc.MAX_PER_PASS, log=printf)
            return {"mode": "auto", "removed": len(res["removed"])}
        rep = scan(srv)
        return {"mode": "ask", "notified": maybe_notify(srv, rep)}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


async def run_loop(srv, first_delay: float = FIRST_DELAY_S) -> None:
    """:func:`pass_once` after ``first_delay`` and then every
    :data:`INTERVAL_S`, on the ticks' own pool (``srv._in_bg_pool``)."""
    await asyncio.sleep(first_delay)
    while True:
        try:
            await srv._in_bg_pool(pass_once, srv)
        except Exception:  # noqa: BLE001 — the pass must never die
            pass
        await asyncio.sleep(INTERVAL_S)
