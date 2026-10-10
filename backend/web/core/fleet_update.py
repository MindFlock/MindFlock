"""Update all my devices (Settings → Devices, ``mindflock devices update``).

Two halves, the same two audiences as the rest of "Your devices"
(:mod:`backend.web.addons.fleet` routes them):

* **The receiver** (:func:`apply`, ``POST /api/fleet/update/apply``) — another
  member asks THIS device to update its engine. Fleet-key authenticated ONLY
  (never a pasted device token, never the browser ``fwd/`` allow-list, which
  keeps ``/api/update/*`` out on purpose), and it installs only a published
  release tag at or above what runs here (default: the newest release) — one
  member must not be able to downgrade another, or move it onto a branch. A
  dev checkout or an engine not installed by ``install.sh`` answers 409 with
  why (:func:`~backend.web.core.self_update.blocked_reason`), which the
  rollout reports as "skipped", not as a failure. The install itself is the
  same detached :func:`~backend.web.core.self_update.start_update` the
  Settings → Advanced button runs, and the receiver's own watcher
  (:mod:`backend.web.core.update_watch`) restarts it onto the new build.

* **The rollout** (:func:`start`, ``POST /api/fleet/update`` — ``privileged()``
  on the device you are at): every other live member, ONE AT A TIME, then
  this device last. For each it asks for the update, then waits until that
  member's own hello reports the new commit (or version) — the only proof the
  new engine actually booted there — and stops the whole rollout at the first
  member that fails, rolls back (the installer's health check, see
  :mod:`backend.web.core.self_update`) or never comes back: a release that
  can't start on one machine is not pushed to the next. Offline members,
  members already current, and members that can't be updated remotely are
  skipped with the reason. The desktop shell is not updated from here (it
  updates itself on its next launch); a member whose shell lags says so.

Progress lives in ``<config dir>/fleet_update.json`` so the screen still shows
how it went after this device restarts onto the new build at the end. That
last step keeps the rollout ``running`` until this device's own
``update.json`` settles it — done once the new build answers here, halted
when it failed or rolled back (:func:`status` finishes it in the restarted
process).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import List, Optional, Tuple

from backend import log
from backend.config import config as _config
from backend.web.core import self_update as _self_update

#: Longest a member may take from "asked" to "its hello reports the new
#: build": the whole install (a cold uv cache on a slow link) plus a restart.
MEMBER_TIMEOUT_S = _self_update.INSTALL_TIMEOUT_S + 5 * 60

#: After a member's install reports done, how long its restart may take
#: before the rollout calls it failed (its installer rolls back after 90 s).
RESTART_GRACE_S = 4 * 60

#: Seconds between looks at the member being updated.
POLL_S = 5.0

#: How long the apply request may take (it resolves the tag on the member).
APPLY_TIMEOUT_S = 90.0

#: Steps a member row goes through. ``current``/``skipped`` are not failures.
STEPS = (
    "queued",
    "updating",
    "restarting",
    "done",
    "current",
    "skipped",
    "failed",
    "not_started",
)

_LOCK = asyncio.Lock()
_TASK: dict = {"task": None}

_BLOCKED_COPY = {
    "editable": "dev checkout — `git pull` there, then restart it",
    "other": "not installed by install.sh — update it there",
}


def _path() -> Path:
    return Path(_config.GetConfigDir()) / "fleet_update.json"


def _log(fmt: str, *args) -> None:
    if log.ErrorLog is not None:
        try:
            log.ErrorLog.Printf(fmt, *args)
        except Exception:  # noqa: BLE001
            pass


def _idle() -> dict:
    return {"state": "idle", "tag": "", "version": "", "members": [], "error": ""}


def _read() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _idle()
    return data if isinstance(data, dict) and data.get("state") else _idle()


def _write(doc: dict) -> None:
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as err:
        _log("fleet update: could not save progress (%v)", err)


def _running_here() -> bool:
    task = _TASK.get("task")
    return task is not None and not task.done()


_PENDING = ("updating", "restarting")


def _self_row(doc: dict) -> Optional[dict]:
    return next((r for r in doc.get("members") or [] if r.get("self")), None)


def _settle_self(doc: dict, row: dict) -> bool:
    """Settle THIS device's row from its own ``update.json``: ``done`` once
    this process runs the installed build, ``failed`` when the install failed
    or was rolled back (the rollout then reads halted). False while it is
    still on its way (installing, or installed and restarting)."""
    st = _self_update.read_state()
    state = st.get("state")
    if (state == "done" and _self_update.applied(st)) or (
        _self_update.installed_version() == doc.get("version")
    ):
        row.update(step="done", detail="")
        return True
    if state in ("failed", "rolled_back"):
        detail = (
            "v%s didn't start here — it was rolled back" % doc.get("version")
            if state == "rolled_back"
            else str(st.get("error") or "the install failed here")
        )
        row.update(step="failed", detail=detail)
        if doc.get("state") == "running":
            doc.update(
                state="halted",
                error="this device: %s" % detail,
                finished_at=time.time(),
            )
        return True
    return False


def _self_underway() -> bool:
    """Whether this device's own update is still on its way: installing, or
    installed and not yet restarted onto (within :data:`RESTART_GRACE_S`)."""
    st = _self_update.read_state()
    if st.get("state") == "started":
        return _self_update.running()
    if st.get("state") == "done" and not _self_update.applied(st):
        finished = float(st.get("finished_at") or 0)
        return (time.time() - finished) < RESTART_GRACE_S
    return False


def _caught_up(doc: dict) -> dict:
    """``{key: version}`` for every row a halted rollout left behind, when
    each of them now runs its version anyway (updated by hand there, or by a
    later rollout from another device) — else ``{}``. Members by their last
    hello, this device by what it runs; an unreachable or unreadable one
    proves nothing, so the record stays halted."""
    from backend.web.core import fleet as _fleet

    version = str(doc.get("version") or "")
    behind = [
        r
        for r in doc.get("members") or []
        if r.get("step") in ("queued", "failed", "not_started") + _PENDING
    ]
    if not version or not behind:
        return {}
    now = {}
    for row in behind:
        if row.get("self"):
            theirs = _self_update.installed_version()
        else:
            dev = _fleet._device(str(row.get("key") or "")) or {}
            theirs = str(dev.get("version") or "") if dev.get("reachable") else ""
        if not theirs or _self_update.is_newer(version, theirs):
            return {}
        now[row.get("key")] = theirs
    return now


def status() -> dict:
    """The rollout as the screen shows it. This device's own row (last, and
    the one update that restarts the process running the rollout) follows its
    ``update.json``: ``done`` once this process runs the target build,
    ``failed`` — and the rollout halted — when it failed or rolled back, and
    still ``running`` while it installs and restarts. Any other ``running``
    record with no task behind it was cut off by a restart of THIS device
    mid-way — said as halted, not left spinning. A ``halted`` record reads as
    done once every device it left behind runs the target anyway: "stopped
    updating your devices" must not outlive the devices being behind."""
    doc = _read()
    changed = False
    me = _self_row(doc)
    if me is not None and me.get("step") in _PENDING:
        changed = _settle_self(doc, me) or changed
    if doc.get("state") == "running" and not _running_here():
        others_left = any(
            r.get("step") in ("queued",) + _PENDING
            for r in doc.get("members") or []
            if not r.get("self")
        )
        if me is not None and me.get("step") in _PENDING and not others_left:
            if not _self_underway():
                me.update(
                    step="failed",
                    detail="installed, but it didn't come back on v%s"
                    % doc.get("version"),
                )
                doc.update(
                    state="halted",
                    error="this device: %s" % me["detail"],
                    finished_at=time.time(),
                )
                changed = True
        elif others_left:
            doc.update(
                state="halted",
                error=doc.get("error") or "interrupted — this device restarted mid-way",
                finished_at=doc.get("finished_at") or time.time(),
            )
            for row in doc.get("members") or []:
                if row.get("step") in ("queued",) + _PENDING and not row.get("self"):
                    row["step"] = "not_started" if row["step"] == "queued" else "failed"
            changed = True
        else:
            doc.update(state="done", finished_at=doc.get("finished_at") or time.time())
            changed = True
    if doc.get("state") == "halted" and not _running_here():
        now = _caught_up(doc)
        if now:
            for row in doc.get("members") or []:
                if row.get("key") in now:
                    row.update(step="current", detail="on v%s now" % now[row["key"]])
            doc.update(state="done", error="")
            changed = True
    if changed:
        _write(doc)
    return doc


def _shell_note(host: str, shell: str, version: str) -> str:
    if shell and _self_update.is_newer(version, shell):
        return "desktop app on %s (v%s) updates on its next launch" % (host, shell)
    return ""


# --------------------------------------------------------------------------- #
# The receiver
# --------------------------------------------------------------------------- #
async def apply(body: Optional[dict]) -> Tuple[dict, int]:
    """Another member asked this device to update (fleet key already
    checked by the route). Returns ``(payload, status)``."""
    tag = str((body or {}).get("tag") or "").strip()
    if not tag:
        release = await _self_update.latest_release()
        if not release:
            return {
                "ok": False,
                "error": "couldn't reach GitHub for the newest release",
            }, 502
        tag = str(release.get("tag") or "")
    reason, status = await _self_update.check_remote_ref(tag)
    if reason:
        return {"ok": False, "error": reason}, status
    blocked = _self_update.blocked_reason()
    if blocked:
        return {
            "ok": False,
            "blocked": True,
            "install": _self_update.install_kind(),
            "error": blocked,
        }, 409
    current = _self_update.installed_version()
    if current and not _self_update.is_newer(tag, current):
        return {"ok": True, "current": True, "version": current}, 200
    result = await asyncio.to_thread(_self_update.start_update, tag)
    if not result.get("ok"):
        busy = "already running" in str(result.get("error") or "")
        return result, 409 if busy else 400
    return result, 200


def member_state() -> dict:
    """What a rollout asks a member while it updates (fleet key only):
    its version and commit, how it is installed, and the update state."""
    _self_update.running()  # settles a dead installer as interrupted
    st = _self_update.read_state()
    return {
        "version": _self_update.installed_version(),
        "commit": _self_update.installed_commit(),
        "install": _self_update.install_kind(),
        "blocked": _self_update.blocked_reason(),
        "state": st.get("state", "idle"),
        "ref": st.get("ref", ""),
        "error": st.get("error", ""),
        "restart_pending": _self_update.restart_pending(st),
    }


# --------------------------------------------------------------------------- #
# The rollout
# --------------------------------------------------------------------------- #
async def _probe(key: str) -> Optional[dict]:
    """``key``'s discovery record, re-probed now (fresh hello)."""
    from backend.web.core import fleet as _fleet

    return await _fleet._refresh(key)


def _hello_matches(dev: Optional[dict], version: str, commit: str) -> bool:
    """Whether ``dev``'s hello says it runs the target build."""
    if not dev or not dev.get("reachable"):
        return False
    theirs = str(dev.get("commit") or "")
    if commit and theirs:
        return theirs == commit
    return str(dev.get("version") or "") == version


async def _update_member(doc: dict, row: dict, tag: str, version: str) -> bool:
    """Drive one member to ``version``. Returns False to halt the rollout."""
    from backend.web.core import fleet as _fleet
    from backend.web.core import remote as _remote

    def step(name: str, detail: str = "") -> None:
        row.update(step=name, detail=detail, at=time.time())
        _write(doc)

    def note_shell(dev: dict) -> None:
        # A hello right after a restart may not know the desktop app yet ("")
        # — keep what the member said before rather than forget it.
        row["shell_version"] = str(dev.get("shell_version") or "") or str(
            row.get("shell_version") or ""
        )

    host = row["host"]
    dev = await _probe(row["key"])
    if not dev or not dev.get("reachable"):
        step("skipped", "offline")
        return True
    note_shell(dev)
    theirs = str(dev.get("version") or "")
    if theirs and not _self_update.is_newer(version, theirs):
        step("current", "already on v%s" % theirs)
        return True
    if not _fleet.member_device(dev):
        # The fleet key only ever goes to the member under its recorded name.
        step("skipped", "not reachable under its recorded name")
        return True
    install = str(dev.get("install") or "")
    if install in _BLOCKED_COPY:
        step("skipped", _BLOCKED_COPY[install])
        return True
    status, resp = await _remote.post_json(
        dev,
        "/api/fleet/update/apply",
        {"tag": tag},
        timeout=APPLY_TIMEOUT_S,
        bearer=_fleet.fleet_key(),
    )
    resp = resp if isinstance(resp, dict) else {}
    if status in _fleet.TOO_OLD:
        step(
            "skipped", "its MindFlock is too old to update from here — update it there"
        )
        return True
    if status == 409 and resp.get("blocked"):
        install = str(resp.get("install") or "")
        step("skipped", _BLOCKED_COPY.get(install) or str(resp.get("error") or ""))
        return True
    if status != 200 or not resp.get("ok"):
        step("failed", str(resp.get("error") or _fleet._err_text(status, resp, dev)))
        return False
    if resp.get("current"):
        step("current", "already on v%s" % (resp.get("version") or version))
        return True
    commit = str(resp.get("commit") or "")
    step("updating")
    deadline = time.monotonic() + MEMBER_TIMEOUT_S
    done_at = 0.0
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_S)
        dev = await _probe(row["key"]) or dev
        if _hello_matches(dev, version, commit):
            note_shell(dev)
            step("done", _shell_note(host, row["shell_version"], version))
            return True
        if not _fleet.member_device(dev):
            # Re-checked on every look, not just before the apply: the key
            # only ever goes to the member under its recorded name, and a
            # restart is exactly when another node could answer for it.
            continue
        st_status, st = await _remote.get_json(
            dev, "/api/fleet/update/state", bearer=_fleet.fleet_key()
        )
        st = st if isinstance(st, dict) else {}
        if st_status == 200:
            state = st.get("state")
            if state == "rolled_back":
                step("failed", "v%s didn't start there — it was rolled back" % version)
                return False
            if state == "failed":
                step("failed", str(st.get("error") or "the install failed there"))
                return False
            if state == "done" and not done_at:
                done_at = time.monotonic()
                step("restarting")
        if done_at and time.monotonic() - done_at > RESTART_GRACE_S:
            step("failed", "installed, but it didn't come back on v%s" % version)
            return False
    step("failed", "timed out waiting for v%s there" % version)
    return False


async def _run(doc: dict) -> None:
    tag, version = doc["tag"], doc["version"]
    halted = ""
    for row in doc["members"]:
        if row.get("self"):
            continue
        try:
            ok = await _update_member(doc, row, tag, version)
        except Exception as err:  # noqa: BLE001 — one member never kills the run
            row.update(step="failed", detail=str(err)[:200])
            ok = False
        if not ok:
            halted = "%s: %s" % (row["host"], row.get("detail") or "failed")
            break
    me = next((r for r in doc["members"] if r.get("self")), None)
    if halted:
        for row in doc["members"]:
            if row.get("step") == "queued":
                row["step"] = "not_started"
        doc.update(state="halted", error=halted, finished_at=time.time())
        _write(doc)
        return
    if me is not None:
        current = _self_update.installed_version()
        blocked = _self_update.blocked_reason()
        if not _self_update.is_newer(version, current):
            me.update(step="current", detail="already on v%s" % current)
        elif blocked:
            install = _self_update.install_kind()
            me.update(step="skipped", detail=_BLOCKED_COPY.get(install) or blocked)
        else:
            result = await asyncio.to_thread(_self_update.start_update, tag)
            if result.get("ok"):
                me.update(
                    step="updating", detail="this device restarts once it's installed"
                )
                _write(doc)
                await _await_self(doc, me)
            else:
                me.update(step="failed", detail=str(result.get("error") or ""))
                doc.update(state="halted", error="this device: %s" % me["detail"])
    if doc.get("state") == "running":
        doc.update(state="done", finished_at=time.time())
    _write(doc)


async def _await_self(doc: dict, me: dict) -> None:
    """Keep the rollout ``running`` while THIS device installs: settled by
    its ``update.json`` (failed / rolled back halts the rollout). Once the
    install is done the watcher re-execs this process — which ends this task
    mid-wait, on purpose; :func:`status` in the new process finishes the
    rollout from the same file."""
    deadline = time.monotonic() + MEMBER_TIMEOUT_S
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_S)
        if _settle_self(doc, me):
            return
        if (
            me.get("step") == "updating"
            and _self_update.read_state().get("state") == "done"
        ):
            me.update(step="restarting", detail="")
            _write(doc)
    me.update(step="failed", detail="timed out waiting for v%s here" % doc["version"])
    doc.update(state="halted", error="this device: %s" % me["detail"])


async def start(tag: str = "") -> Tuple[dict, int]:
    """Begin a rollout to ``tag`` (default: the newest release). Returns
    ``(status payload, http status)``; 409 while one is already running."""
    from backend.web.core import fleet as _fleet

    async with _LOCK:
        if _running_here():
            return {
                **status(),
                "error": "an update of your devices is already running",
            }, 409
        if not _fleet.in_fleet():
            return {"error": "this device isn't in a group of devices"}, 400
        tag = str(tag or "").strip()
        if not tag:
            release = await _self_update.latest_release()
            if not release:
                return {"error": "couldn't reach GitHub for the newest release"}, 502
            tag = str(release.get("tag") or "")
        reason, code = await _self_update.check_remote_ref(tag)
        if reason:
            return {"error": reason}, code
        me = _fleet._self_key()
        rows: List[dict] = []
        for key, m in sorted(
            _fleet.live_members().items(), key=lambda kv: (kv[0] == me, kv[0])
        ):
            rows.append(
                {
                    "key": key,
                    "host": (_fleet._self_host() if key == me else "")
                    or str(m.get("host") or key),
                    "self": key == me,
                    "step": "queued",
                    "detail": "",
                }
            )
        doc = {
            "state": "running",
            "tag": tag,
            "version": tag.lstrip("vV"),
            "started_at": time.time(),
            "finished_at": 0,
            "error": "",
            "members": rows,
        }
        _write(doc)
        _TASK["task"] = asyncio.ensure_future(_run(doc))
        return doc, 200
