"""The first-run steps — ONE definition for every surface that shows them.

The web UI's setup checklist (the empty-grid card, the Setup modal, the
"Setup 3/5" chip), ``GET /api/setup`` and the ``mindflock init`` wizard all
read :data:`STEPS` and the same stored state, so they can never disagree about
what a step is called or whether it is done.

The steps come in three groups (``group``), and every surface draws them as
three groups:

* ``start`` — **Get started**, the three REQUIRED steps to a running agent:
  ``tools``, ``agent_login``, ``repo``. ``summary.required_done`` is these.
* ``connect`` — **Connect your work**, optional but counted (the chip's "N/5"):
  ``github``, ``tracker``. Each can be skipped.
* ``later`` — **Optional, later**: ``phone``. Never part of getting started:
  not counted in ``summary`` (so it never keeps the chip up), drawn folded.

State lives in settings as ``general.setup``::

    {"tools":       {"done": bool, "at": iso|null},
     "agent_login": {"done": bool, "at": iso|null},
     "repo":        {"done": bool, "at": iso|null, "started": iso|null},
     "github":      {"done": bool, "at": iso|null, "skipped": bool},
     "tracker":     {"done": bool, "at": iso|null, "skipped": bool},
     "phone":       {"done": bool, "at": iso|null, "skipped": bool}}

Only the backend writes it, and only forwards: a step that was once done stays
done (the doctor list in step 1 still shows a tool that went missing).
``general.onboarded`` is derived from ``repo.done`` by the settings layer so
settings sync and every existing reader keep working. A store from before
``github`` was its own step (``tracker`` was "Connect a tracker and GitHub")
reads ``github`` as skipped once ``tracker`` was done or skipped, so an
upgrade never re-asks a veteran.

Who marks what:

* ``tools`` / ``agent_login`` — :func:`note_doctor`, called by every doctor run
  (:func:`backend.doctor.run_checks`): tools when no required check fails,
  agent login when the default agent CLI is installed and its login probe
  found evidence (or has nothing to look for).
* ``repo`` — done only once a session's agent has actually WORKED on
  something (:func:`note_agent_worked`, from the activity classifier's first
  corroborated ``working`` reading). The first session create
  (``server._mark_onboarded`` → ``mark("repo")``) only records ``started``: a
  session sitting at a sign-in prompt is not a first agent, and calling it one
  told a new user they had succeeded when nothing ran.
* ``github`` — :func:`status` (every ``GET /api/setup``) when a GitHub
  credential resolves: a Settings / env token, or the doctor's signed-in gh.
* ``tracker`` — saving the first ticket source (``PUT /api/settings/ticketing/sources``).
* ``phone`` — a QR fetch by the owner (``GET /api/mobile``) or a phone loading
  ``/m`` from off this machine.
* ``POST /api/setup/<step>/skip`` — the optional steps.

Nothing here raises: every writer is best-effort, because none of the call
sites (a session create, a settings save, the doctor) may fail over a
bookkeeping flag.
"""

from __future__ import annotations

import datetime as _dt
import shutil
import threading
from typing import Any, Dict, Iterable, List, Optional

__all__ = [
    "STEPS",
    "STEP_IDS",
    "REQUIRED",
    "OPTIONAL",
    "LATER",
    "COUNTED",
    "TITLES",
    "empty_state",
    "normalize",
    "state",
    "mark",
    "note_agent_worked",
    "skip",
    "checklist",
    "summary",
    "note_doctor",
    "github_connected",
    "provider_logins",
    "set_first_run_report",
    "first_run_report",
    "is_required_check",
    "tag_required",
    "tools_title",
    "status",
]

#: The checklist, in the order every surface shows it. ``group``: ``start``
#: (required), ``connect`` (optional, counted), ``later`` (optional, uncounted).
STEPS = (
    {"id": "tools", "title": "Install the tools", "required": True, "group": "start"},
    {
        "id": "agent_login",
        "title": "Sign in to your agent",
        "required": True,
        "group": "start",
    },
    {
        "id": "repo",
        "title": "Start your first agent",
        "required": True,
        "group": "start",
    },
    {"id": "github", "title": "Connect GitHub", "required": False, "group": "connect"},
    {
        "id": "tracker",
        "title": "Connect your tracker",
        "required": False,
        "group": "connect",
    },
    {
        "id": "phone",
        "title": "Use it from your phone",
        "required": False,
        "group": "later",
    },
)
STEP_IDS = tuple(str(s["id"]) for s in STEPS)
REQUIRED = tuple(str(s["id"]) for s in STEPS if s["required"])
OPTIONAL = tuple(str(s["id"]) for s in STEPS if not s["required"])
#: Optional extras that are never part of getting started: drawn folded under
#: "Optional, later" and left out of :func:`summary`.
LATER = tuple(str(s["id"]) for s in STEPS if s["group"] == "later")
#: What :func:`summary` counts (the chip's "N/5").
COUNTED = tuple(sid for sid in STEP_IDS if sid not in LATER)
#: Steps a plain :func:`mark` only STARTS: done takes the thing itself
#: (``repo``: an agent that worked — :func:`note_agent_worked`).
EVIDENCE_STEPS = ("repo",)
GROUPS = {str(s["id"]): str(s["group"]) for s in STEPS}
TITLES = {str(s["id"]): str(s["title"]) for s in STEPS}

_GLYPH = {"done": "✓", "skipped": "–", "todo": "•"}

#: Doctor rows that are optional extras (phone/tailnet access, peer links, a
#: local model, warm caches, the clipboard, the gh CLI — a token does its job).
#: "Install the tools" neither waits on them nor shows them; Settings → Doctor
#: lists them. Mirrored by EXTRA_CHECKS in frontend setupModel.ts (a test pins
#: the two together).
EXTRA_CHECKS = frozenset(
    {
        "tailscale",
        "bwrap",
        "cloudflared",
        "local-model",
        "cache-seeds",
        "tailnet-mtu",
        "clipboard",
        "gh",
    }
)


# --------------------------------------------------------------------------- #
# State shape
# --------------------------------------------------------------------------- #
def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def empty_state() -> Dict[str, dict]:
    """Every step, nothing done."""
    out: Dict[str, dict] = {}
    for sid in STEP_IDS:
        entry: dict = {"done": False, "at": None}
        if sid in OPTIONAL:
            entry["skipped"] = False
        if sid in EVIDENCE_STEPS:
            entry["started"] = None
        out[sid] = entry
    return out


def normalize(raw: Any) -> Dict[str, dict]:
    """A full, well-typed state from whatever is stored (tolerant: a non-dict,
    unknown steps or odd values read as "not done")."""
    out = empty_state()
    if not isinstance(raw, dict):
        return out
    for sid in STEP_IDS:
        v = raw.get(sid)
        if not isinstance(v, dict):
            continue
        done = bool(v.get("done", False))
        at = v.get("at")
        out[sid]["done"] = done
        out[sid]["at"] = str(at) if (done and at) else None
        if sid in OPTIONAL:
            out[sid]["skipped"] = bool(v.get("skipped", False))
        if sid in EVIDENCE_STEPS:
            started = v.get("started")
            out[sid]["started"] = str(started) if started else (out[sid]["at"])
    # ``github`` used to be half of "Connect a tracker and GitHub": a store
    # that settled that step before ``github`` existed has settled GitHub too.
    if "github" not in raw and not out["github"]["done"]:
        t = out["tracker"]
        if t["done"] or t.get("skipped"):
            out["github"]["skipped"] = True
    return out


def compact(full: Dict[str, dict]) -> Dict[str, dict]:
    """The on-disk form: ``{}`` while nothing is done or skipped (so an
    untouched store serializes unchanged), else every step."""
    full = normalize(full)
    if not any(
        v["done"] or v.get("skipped") or v.get("started") for v in full.values()
    ):
        return {}
    out: Dict[str, dict] = {}
    for sid in STEP_IDS:
        v = full[sid]
        entry: dict = {"done": bool(v["done"]), "at": v["at"]}
        if sid in OPTIONAL and v.get("skipped"):
            entry["skipped"] = True
        if sid in EVIDENCE_STEPS and v.get("started"):
            entry["started"] = v["started"]
        out[sid] = entry
    return out


# --------------------------------------------------------------------------- #
# Reading + writing the store
# --------------------------------------------------------------------------- #
def state() -> Dict[str, dict]:
    """The stored checklist state (never raises: unreadable = nothing done)."""
    try:
        from backend.config.settings import load_settings

        return normalize(load_settings().general.setup)
    except Exception:  # noqa: BLE001 — advisory flags only
        return empty_state()


def _write(full: Dict[str, dict]) -> bool:
    try:
        from backend.config import settings as settings_mod

        settings_mod.update_settings(general={"setup": compact(full)})
        return True
    except Exception:  # noqa: BLE001 — a store we can't write costs the flag only
        return False


def mark(step: str, *, done: bool = True) -> bool:
    """Record ``step`` as done. Forward-only and idempotent; returns whether
    the store changed. Unknown steps are ignored.

    An :data:`EVIDENCE_STEPS` step (``repo``) is only STARTED here — the
    session exists; :func:`note_agent_worked` is what finishes it."""
    if step not in STEP_IDS or not done:
        return False
    cur = state()
    if cur[step]["done"]:
        return False
    if step in EVIDENCE_STEPS:
        if cur[step].get("started"):
            return False
        cur[step]["started"] = _now()
        return _write(cur)
    cur[step]["done"] = True
    cur[step]["at"] = _now()
    if step in OPTIONAL:
        cur[step]["skipped"] = False
    return _write(cur)


#: Set once this process has seen the repo step done (or written it), so the
#: activity classifier's hot path stops asking.
_worked_noted = False


def note_agent_worked() -> bool:
    """A session's agent was seen WORKING (corroborated — a hook, its live
    status line, sustained CPU): the first agent really started on its task,
    so ``repo`` is done. Cheap after the first call (a process flag); returns
    whether the store changed. Never raises."""
    global _worked_noted
    if _worked_noted:
        return False
    try:
        cur = state()
        if cur["repo"]["done"]:
            _worked_noted = True
            return False
        now = _now()
        cur["repo"]["done"] = True
        cur["repo"]["at"] = now
        cur["repo"]["started"] = cur["repo"].get("started") or now
        changed = _write(cur)
        if changed:
            _worked_noted = True
        return changed
    except Exception:  # noqa: BLE001 — a bookkeeping flag
        return False


def skip(step: str) -> bool:
    """Mark an OPTIONAL step skipped (a done step stays done). Returns whether
    the store changed; a required or unknown step is refused (``False``)."""
    if step not in OPTIONAL:
        return False
    cur = state()
    if cur[step]["done"] or cur[step].get("skipped"):
        return False
    cur[step]["skipped"] = True
    return _write(cur)


def unskip(step: str) -> bool:
    """Undo a skip (the UI's "show it again")."""
    if step not in OPTIONAL:
        return False
    cur = state()
    if not cur[step].get("skipped"):
        return False
    cur[step]["skipped"] = False
    return _write(cur)


# --------------------------------------------------------------------------- #
# The checklist as every surface draws it
# --------------------------------------------------------------------------- #
def checklist(current: Optional[Dict[str, dict]] = None) -> List[dict]:
    """``[{id, title, required, group, done, at, skipped, status}]`` in order.
    ``status`` is ``done`` | ``skipped`` | ``todo``; ``group`` is ``start`` |
    ``connect`` | ``later``."""
    cur = normalize(current) if current is not None else state()
    out: List[dict] = []
    for s in STEPS:
        sid = str(s["id"])
        v = cur[sid]
        skipped = bool(v.get("skipped")) and not v["done"]
        out.append(
            {
                "id": sid,
                "title": s["title"],
                "required": bool(s["required"]),
                "group": str(s["group"]),
                "done": bool(v["done"]),
                "at": v["at"],
                "skipped": skipped,
                "status": "done" if v["done"] else ("skipped" if skipped else "todo"),
                **(
                    {"started": bool(v.get("started")) and not v["done"]}
                    if sid in EVIDENCE_STEPS
                    else {}
                ),
            }
        )
    return out


def summary(steps: Optional[Iterable[dict]] = None) -> dict:
    """``{done, total, required_done, complete, next}`` — the chip's "3/5",
    whether the grid card may go, and whether the chip may go too. Counts the
    ``start`` and ``connect`` steps only: an "Optional, later" extra (the
    phone) never keeps the chip up or becomes ``next``."""
    rows = [
        r
        for r in (list(steps) if steps is not None else checklist())
        if r.get("group", GROUPS.get(str(r.get("id")), "")) != "later"
    ]
    done = sum(1 for r in rows if r["done"] or r["skipped"])
    required_done = all(r["done"] for r in rows if r["required"])
    todo = [r["id"] for r in rows if r["status"] == "todo"]
    return {
        "done": done,
        "total": len(rows),
        "required_done": required_done,
        "complete": not todo,
        "next": todo[0] if todo else "",
    }


def glyph(status: str) -> str:
    """✓ / – / • for a step status (the wizard's rows)."""
    return _GLYPH.get(status, "?")


def format_lines(rows: Optional[List[dict]] = None) -> List[str]:
    """The checklist as ``mindflock init`` prints it: one line per step."""
    rows = rows if rows is not None else checklist()
    width = max(len(r["title"]) for r in rows)
    out = []
    for n, r in enumerate(rows, 1):
        later = r.get("group") == "later"
        note = {
            "done": "done",
            "skipped": "skipped",
            "todo": (
                "" if r["required"] else ("optional, later" if later else "optional")
            ),
        }[r["status"]]
        line = "  %s %d. %s" % (glyph(r["status"]), n, r["title"].ljust(width))
        out.append((line + "  " + note).rstrip())
    return out


# --------------------------------------------------------------------------- #
# Who marks what: the doctor
# --------------------------------------------------------------------------- #
def _by_id(checks: Iterable[Any]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for c in checks or ():
        d = c if isinstance(c, dict) else getattr(c, "__dict__", None)
        if d is None and hasattr(c, "to_dict"):
            d = c.to_dict()
        if isinstance(d, dict) and d.get("id"):
            out[str(d["id"])] = d
    return out


def doctor_progress(checks: Iterable[Any]) -> Dict[str, bool]:
    """What a doctor run says about the first two steps: ``tools`` when no
    check fails other than an optional extra (:data:`EXTRA_CHECKS` — a broken
    Tailscale is not a reason getting started is unfinished; there must be
    checks at all), ``agent_login`` when
    the default agent CLI is installed and its auth row is not a warning (a
    provider that declares no credential locations has nothing to check, which
    counts as signed in — the same reading Settings → Doctor gives it)."""
    by = _by_id(checks)
    if not by:
        return {"tools": False, "agent_login": False}
    tools = all(
        c.get("status") != "fail" for cid, c in by.items() if cid not in EXTRA_CHECKS
    )
    cli = by.get("agent-cli") or {}
    auth = by.get("agent-auth")
    agent = (
        cli.get("status") not in ("fail", None)
        and auth is not None
        and auth.get("status") not in ("warn", "fail")
    )
    return {"tools": bool(tools), "agent_login": bool(agent)}


def is_required_check(check_id: str) -> bool:
    """Whether a doctor row can block getting started (steps ①–③): every row
    but an optional extra (:data:`EXTRA_CHECKS`). The same split
    :func:`doctor_progress` waits on, so "All checks" in the setup checklist
    lists exactly what ``tools`` waits for."""
    return str(check_id or "") not in EXTRA_CHECKS


def tag_required(checks: Iterable[Any]) -> List[dict]:
    """``checks`` (dicts, as served) with ``required`` set on each row that
    does not already carry one (a newer doctor may set its own). Never
    raises; a row that isn't a dict passes through untouched."""
    out: List[dict] = []
    for c in checks or ():
        if isinstance(c, dict) and "required" not in c:
            c = {**c, "required": is_required_check(str(c.get("id") or ""))}
        out.append(c)
    return out


def tools_title(checks: Iterable[Any], default_provider: str = "") -> str:
    """Step ①'s name: "Install Claude Code" — the default agent by its product
    name, since that is what a first-run computer is missing — unless the
    agent is already here and something else is not (then the generic
    "Install the tools", so the title never names a thing that is done)."""
    by = _by_id(checks)
    cli = by.get("agent-cli")
    others_missing = any(
        c.get("status") == "fail" and cid not in EXTRA_CHECKS and cid != "agent-cli"
        for cid, c in by.items()
    )
    if cli is not None and cli.get("status") == "ok" and others_missing:
        return TITLES["tools"]
    name = default_provider or str((cli or {}).get("provider") or "") or "claude"
    try:
        from backend.web.addons.connections import agent_product_name

        return "Install " + agent_product_name(name)
    except Exception:  # noqa: BLE001 — a title must never break the payload
        return TITLES["tools"]


def note_doctor(checks: Iterable[Any]) -> None:
    """The doctor's hook: mark ``tools`` / ``agent_login`` from a run's results.
    Never raises."""
    try:
        prog = doctor_progress(checks)
        for sid, ok in prog.items():
            if ok:
                mark(sid)
    except Exception:  # noqa: BLE001 — the doctor must never fail over a flag
        pass


# --------------------------------------------------------------------------- #
# Who marks what: a GitHub credential
# --------------------------------------------------------------------------- #
def github_connected(checks: Iterable[Any] = ()) -> bool:
    """Whether a GitHub credential resolves without shelling out: a Settings
    or env token (:func:`backend.web.core.github_auth.resolve_token` with
    ``gh=False``), or the doctor's ``gh`` row reading signed in (``ok``).
    Never raises."""
    try:
        if (_by_id(checks).get("gh") or {}).get("status") == "ok":
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.web.core import github_auth

        source, _tok = github_auth.resolve_token(gh=False)
        return source != "none"
    except Exception:  # noqa: BLE001 — unknowable reads as "not yet"
        return False


# --------------------------------------------------------------------------- #
# Per-provider login state (step 2's one-button-per-installed-CLI)
# --------------------------------------------------------------------------- #
def provider_logins() -> List[dict]:
    """``[{name, installed, logged_in, login_cmd, default}]`` for every
    registered agent CLI. ``logged_in`` is ``True`` on evidence, ``False`` when
    the CLI names credential locations and none exist, ``None`` when it names
    nothing (unknowable). Never raises."""
    out: List[dict] = []
    try:
        from backend import doctor, providers

        default = doctor._default_provider_name()
        for p in providers.all_providers():
            if p.name == "generic":
                continue
            try:
                binary = doctor._resolve_agent_binary(p.name)
                installed = bool(shutil.which(binary))
                logged: Optional[bool] = None
                if installed:
                    if doctor._auth_evidence(p):
                        logged = True
                    elif doctor._declares_auth_sources(p):
                        logged = False
                out.append(
                    {
                        "name": p.name,
                        "installed": installed,
                        "logged_in": logged,
                        "login_cmd": (
                            doctor._declared_login_command(p) if installed else ""
                        ),
                        "default": p.name == default,
                    }
                )
            except Exception:  # noqa: BLE001 — one odd provider, not the list
                continue
    except Exception:  # noqa: BLE001
        return out
    return out


# --------------------------------------------------------------------------- #
# The first-run report (what `mindflock serve` printed at boot)
# --------------------------------------------------------------------------- #
_report_lock = threading.Lock()
_report_text = ""


def set_first_run_report(text: str) -> None:
    """Keep the first-serve report's text so ``GET /api/setup`` can show it
    (the desktop app's server prints it into a log nobody reads)."""
    global _report_text
    with _report_lock:
        _report_text = str(text or "")


def first_run_report() -> str:
    with _report_lock:
        return _report_text


# --------------------------------------------------------------------------- #
# GET /api/setup
# --------------------------------------------------------------------------- #
def status(doctor_payload: Optional[dict] = None) -> dict:
    """The whole checklist payload. ``doctor_payload`` is the cached
    ``/api/doctor`` body when the caller has one (its checks refresh the
    first two steps before they are read); a GitHub credential marks
    ``github`` here too."""
    checks: list = []
    if doctor_payload and isinstance(doctor_payload.get("checks"), list):
        checks = doctor_payload["checks"]
        note_doctor(checks)
    try:
        if github_connected(checks):
            mark("github")
    except Exception:  # noqa: BLE001 — a bookkeeping flag
        pass
    rows = checklist()
    default = ""
    try:
        from backend import doctor

        default = doctor._default_provider_name()
    except Exception:  # noqa: BLE001
        default = ""
    for r in rows:
        if r["id"] == "tools":
            r["title"] = tools_title(checks, default)
    tailscale = None
    if doctor_payload:
        ts = _by_id(doctor_payload.get("checks") or []).get("tailscale")
        if ts is not None:
            tailscale = {
                "status": ts.get("status", ""),
                "detail": ts.get("detail", ""),
                "fix": ts.get("fix", ""),
                "docs": ts.get("docs", ""),
            }
    return {
        "steps": rows,
        "summary": summary(rows),
        "doctor": (
            {
                "ok": bool(doctor_payload.get("ok")),
                "checks": tag_required(doctor_payload.get("checks") or []),
                "install": doctor_payload.get("install")
                or {"steps": [], "packages": []},
            }
            if doctor_payload
            else None
        ),
        "tailscale": tailscale,
        "logins": provider_logins(),
        "default_provider": default,
        "report": first_run_report(),
    }
