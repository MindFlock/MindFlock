"""The five first-run steps — ONE definition for every surface that shows them.

The web UI's setup checklist (the empty-grid card, the Setup modal, the
"Setup 3/5" chip), ``GET /api/setup`` and the ``mindflock init`` wizard all
read :data:`STEPS` and the same stored state, so they can never disagree about
what a step is called or whether it is done.

State lives in settings as ``general.setup``::

    {"tools":       {"done": bool, "at": iso|null},
     "agent_login": {"done": bool, "at": iso|null},
     "repo":        {"done": bool, "at": iso|null},
     "tracker":     {"done": bool, "at": iso|null, "skipped": bool},
     "phone":       {"done": bool, "at": iso|null, "skipped": bool}}

Only the backend writes it, and only forwards: a step that was once done stays
done (the doctor list in step 1 still shows a tool that went missing). The
first three are required; the last two may be skipped (``skipped: true``).
``general.onboarded`` is derived from ``repo.done`` by the settings layer so
settings sync and every existing reader keep working.

Who marks what:

* ``tools`` / ``agent_login`` — :func:`note_doctor`, called by every doctor run
  (:func:`backend.doctor.run_checks`): tools when no required check fails,
  agent login when the default agent CLI is installed and its login probe
  found evidence (or has nothing to look for).
* ``repo`` — the first session create (``server._mark_onboarded``).
* ``tracker`` — saving the first ticket source (``PUT /api/settings/ticketing/sources``).
* ``phone`` — a QR fetch by the owner (``GET /api/mobile``) or a phone loading
  ``/m`` from off this machine.
* ``POST /api/setup/<step>/skip`` — the two optional steps.

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
    "TITLES",
    "empty_state",
    "normalize",
    "state",
    "mark",
    "skip",
    "checklist",
    "summary",
    "note_doctor",
    "provider_logins",
    "set_first_run_report",
    "first_run_report",
    "status",
]

#: The checklist, in the order every surface shows it.
STEPS = (
    {"id": "tools", "title": "Engine and tools", "required": True},
    {"id": "agent_login", "title": "Sign in to your agent", "required": True},
    {"id": "repo", "title": "Pick a repo", "required": True},
    {"id": "tracker", "title": "Connect a tracker and GitHub", "required": False},
    {"id": "phone", "title": "Your phone", "required": False},
)
STEP_IDS = tuple(str(s["id"]) for s in STEPS)
REQUIRED = tuple(str(s["id"]) for s in STEPS if s["required"])
OPTIONAL = tuple(str(s["id"]) for s in STEPS if not s["required"])
TITLES = {str(s["id"]): str(s["title"]) for s in STEPS}

_GLYPH = {"done": "✓", "skipped": "–", "todo": "•"}


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
    return out


def compact(full: Dict[str, dict]) -> Dict[str, dict]:
    """The on-disk form: ``{}`` while nothing is done or skipped (so an
    untouched store serializes unchanged), else every step."""
    full = normalize(full)
    if not any(v["done"] or v.get("skipped") for v in full.values()):
        return {}
    out: Dict[str, dict] = {}
    for sid in STEP_IDS:
        v = full[sid]
        entry: dict = {"done": bool(v["done"]), "at": v["at"]}
        if sid in OPTIONAL and v.get("skipped"):
            entry["skipped"] = True
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
    the store changed. Unknown steps are ignored."""
    if step not in STEP_IDS or not done:
        return False
    cur = state()
    if cur[step]["done"]:
        return False
    cur[step]["done"] = True
    cur[step]["at"] = _now()
    if step in OPTIONAL:
        cur[step]["skipped"] = False
    return _write(cur)


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
    """``[{id, title, required, done, at, skipped, status}]`` in order.
    ``status`` is ``done`` | ``skipped`` | ``todo``."""
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
                "done": bool(v["done"]),
                "at": v["at"],
                "skipped": skipped,
                "status": "done" if v["done"] else ("skipped" if skipped else "todo"),
            }
        )
    return out


def summary(steps: Optional[Iterable[dict]] = None) -> dict:
    """``{done, total, required_done, complete, next}`` — the chip's "3/5",
    whether the grid card may go, and whether the chip may go too."""
    rows = list(steps) if steps is not None else checklist()
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
        note = {
            "done": "done",
            "skipped": "skipped",
            "todo": "" if r["required"] else "optional",
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
    required check fails (and there were checks at all), ``agent_login`` when
    the default agent CLI is installed and its auth row is not a warning (a
    provider that declares no credential locations has nothing to check, which
    counts as signed in — the same reading Settings → Doctor gives it)."""
    by = _by_id(checks)
    if not by:
        return {"tools": False, "agent_login": False}
    tools = all(c.get("status") != "fail" for c in by.values())
    cli = by.get("agent-cli") or {}
    auth = by.get("agent-auth")
    agent = (
        cli.get("status") not in ("fail", None)
        and auth is not None
        and auth.get("status") not in ("warn", "fail")
    )
    return {"tools": bool(tools), "agent_login": bool(agent)}


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
    first two steps before they are read)."""
    if doctor_payload and isinstance(doctor_payload.get("checks"), list):
        note_doctor(doctor_payload["checks"])
    rows = checklist()
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
    default = ""
    try:
        from backend import doctor

        default = doctor._default_provider_name()
    except Exception:  # noqa: BLE001
        default = ""
    return {
        "steps": rows,
        "summary": summary(rows),
        "doctor": (
            {
                "ok": bool(doctor_payload.get("ok")),
                "checks": list(doctor_payload.get("checks") or []),
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
