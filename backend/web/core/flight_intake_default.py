"""An intake source's DEFAULT stops (docs/flights.md "A source's default
stops, edited in the Sky (2026-11-02)").

Every source flies our built-in stops unless it names its own saved stop:
``TicketingSource.plan`` for a ticketing source, ``plan`` in
``github.repo_settings[repo]`` (PRs) / ``github.issue_repo_settings[repo]``
(issues). Editing ONE ticket's flight never changes that — these calls are the
only way the Sky does:

* :func:`resolve` — what a NEW item from the source flies right now, by the
  same chain an intake start uses (``server._intake_plan`` minus a per-start
  pick): the source's own plan when it exists, else the built-in cut at its
  Fast-track depth.
* :func:`set_steps` / :func:`set_plan` / :func:`set_from_flight` — write the
  source's plan field through the same store calls the Intake dialog's saves
  use (``settings.set_ticketing_sources`` / ``update_settings(github=…)``),
  then stamp it for settings sync and run the settings hooks, exactly as
  ``PUT /api/settings/ticketing/sources`` and ``POST /api/settings`` do.

A source is ``{kind, target}``: ``tickets`` + the ticketing source's id (its
provider as a fallback, like every intake lookup), or ``prs`` / ``issues`` +
``owner/repo``. Errors are :class:`flights.FlightError` (``unknown_source``,
``unknown_plan``, ``no_source``, ``bad_kind``, ``settings_unreadable``) and
:class:`flights.PlanError` (a bad step, with its field).
"""

from __future__ import annotations

import copy
from typing import List, Optional, Tuple

from backend.web.core import flights as _flights

#: The source kinds (the Sky intake lane's block kinds).
KINDS = ("tickets", "prs", "issues")
#: A flight subject's kind → the source kind it came in through.
SUBJECT_KIND = {"ticket": "tickets", "pr": "prs", "issue": "issues"}
#: A source kind → the intake kind ``plan_for_depth`` / plans' triggers use.
_INTAKE_KIND = {"tickets": "ticket", "prs": "pr", "issues": "issue"}
_KIND_WORD = {"tickets": "Tickets", "prs": "PRs", "issues": "Issues"}
#: The settings table holding a repo source's ``plan``.
_TABLE = {"prs": "repo_settings", "issues": "issue_repo_settings"}
#: The settings list naming a repo source.
_REPOS = {"prs": "repos", "issues": "issue_repos"}
#: A plan with one of these triggers runs on its own (a schedule, a push to
#: main, a hook): never edited in place from a source's default.
_SELF_TRIGGERED = frozenset({"schedule", "main_push", "main_red", "hook"})


def _settings():
    """``settings.json`` as it is on disk NOW (the cache is per-process; the
    pipeline or a synced device may have written it)."""
    from backend.config import settings as _st

    _st.invalidate()
    return _st.load_settings()


def _strict_settings():
    """The settings for a read-modify-write: raising (409) when the file
    exists but cannot be read, never saving over it."""
    from backend.config import settings as _st

    try:
        return _st.load_settings_strict()
    except _st.SettingsUnreadable as err:
        raise _flights.FlightError("settings_unreadable", 409, str(err)) from err


def _kind(kind) -> str:
    k = str(kind or "").strip().lower()
    if k not in KINDS:
        raise _flights.FlightError("bad_kind", 400, "kind is tickets, prs or issues")
    return k


# --------------------------------------------------------------------------- #
# Finding a source
# --------------------------------------------------------------------------- #
def _ticket_source(st, target: str):
    """``(index, source)`` for a ticketing source: the exact id first, then a
    provider (an unkeyed source), as ``server._source_intake_plan`` matches."""
    sources = list(st.ticketing.sources)
    for match in ("id", "provider"):
        for i, src in enumerate(sources):
            if target and getattr(src, match, "") == target:
                return i, src
    return -1, None


def _repo_key(st, kind: str, target: str) -> str:
    """The configured spelling of ``target`` for a repo source (its watch
    list, else its settings table, case-insensitively), ``""`` for none."""
    want = target.strip().lower()
    if not want:
        return ""
    gh = st.github
    for repo in list(getattr(gh, _REPOS[kind], None) or []):
        if str(repo or "").strip().lower() == want:
            return str(repo).strip()
    for repo in (getattr(gh, _TABLE[kind], None) or {}).keys():
        if str(repo).strip().lower() == want:
            return str(repo)
    return ""


def _provider_label(provider: str) -> str:
    try:
        from backend.ticket_ingestion.providers import provider_meta

        return str((provider_meta(provider) or {}).get("label") or "")
    except Exception:  # noqa: BLE001 — a label, never a refusal
        return ""


def _ticket_label(src) -> str:
    """The lane's ``ticketSourceLabel``: the provider's display name, plus
    `` · <label>`` unless the label already names the provider."""
    provider = str(getattr(src, "provider", "") or "")
    prov = _provider_label(provider) or provider or "Tickets"
    detail = str(getattr(src, "label", "") or "").strip()
    if not detail:
        return prov
    low = detail.lower()
    for n in (prov, provider):
        if n and low.startswith(n.lower()):
            nxt = detail[len(n) : len(n) + 1]
            if not (nxt.isascii() and nxt.isalnum()):
                return detail
    return "%s · %s" % (prov, detail)


class _Source:
    """One configured source: what it is called and where its plan lives."""

    def __init__(self, kind: str, target: str, label: str, plan: str, depth: str):
        self.kind = kind
        self.target = target
        self.label = label
        self.plan = plan
        self.depth = depth


def _find(st, kind: str, target: str) -> Optional[_Source]:
    from backend.web import server as srv

    target = str(target or "").strip()
    if kind == "tickets":
        _, src = _ticket_source(st, target)
        if src is None:
            return None
        key = str(getattr(src, "id", "") or getattr(src, "provider", "") or target)
        return _Source(
            kind,
            key,
            "Tickets · " + _ticket_label(src),
            str(getattr(src, "plan", "") or "").strip(),
            srv._cap_source_depth(getattr(src, "depth", "")),
        )
    repo = _repo_key(st, kind, target)
    if not repo:
        return None
    block = (getattr(st.github, _TABLE[kind], None) or {}).get(repo) or {}
    return _Source(
        kind,
        repo,
        "%s · %s" % (_KIND_WORD[kind], repo),
        str(block.get("plan", "") or "").strip(),
        srv._cap_source_depth(block.get("depth", "")),
    )


def _need(st, kind: str, target: str) -> _Source:
    src = _find(st, _kind(kind), target)
    if src is None:
        raise _flights.FlightError(
            "unknown_source", 404, "no %s source %s is configured" % (kind, target)
        )
    return src


# --------------------------------------------------------------------------- #
# Resolving
# --------------------------------------------------------------------------- #
def _dto(src: _Source) -> dict:
    """The GET shape for ``src``. ``plan_id`` / ``name`` / ``steps`` are what
    a new item flies — ``server._intake_plan`` with no per-start pick; all
    empty when nothing flies (Fast-track off / agent only, no own plan)."""
    from backend.web import server as srv

    own_plan = _flights.get_plan(src.plan) if src.plan else None
    plan_id = srv._intake_plan(_INTAKE_KIND[src.kind], "", "", src.plan, src.depth)
    plan = (
        own_plan
        if own_plan is not None
        else (_flights.get_plan(plan_id) if plan_id else None)
    )
    steps = copy.deepcopy(plan["steps"]) if plan else _agent_only(src)
    return {
        "kind": src.kind,
        "target": src.target,
        "label": src.label,
        "plan_id": plan["id"] if plan else "",
        "name": plan["name"] if plan else ("Just the agent" if steps else ""),
        "own": own_plan is not None,
        "steps": steps,
    }


def _agent_only(src: _Source) -> List[dict]:
    """What a source with no flight (Fast-track off / agent only) runs: the
    kind's built-in agent step alone — the editor's starting point, so the
    owner adds blocks after it rather than to an empty chain."""
    base = _flights.get_plan(_flights._BUILTIN_FOR_KIND.get(_INTAKE_KIND[src.kind], ""))
    agent = next(
        (s for s in (base or {}).get("steps") or [] if s.get("kind") == "agent"), None
    )
    return [dict(copy.deepcopy(agent), id="s1")] if agent else []


def resolve(kind, target) -> dict:
    """``GET /api/flights/intake-default`` → ``{kind, target, label,
    plan_id, name, own, steps}``. 400 ``bad_kind``, 404 ``unknown_source``."""
    return _dto(_need(_settings(), kind, target))


def source_of_flight(flight: dict) -> Tuple[str, str]:
    """``(kind, target)`` a flight's default belongs to: an editor chain's
    ``edits_default``, else its intake ``subject`` (ticket → tickets + its
    ticketing source; pr / issue → prs / issues + the repo). ``("", "")``
    for a hand-made flight."""
    ref = _flights.normalize_edits_default(flight.get("edits_default"))
    if ref is not None:
        return ref["kind"], ref["target"]
    subject = flight.get("subject") or {}
    kind = SUBJECT_KIND.get(str(subject.get("kind") or ""), "")
    target = str(subject.get("source") or "").strip()
    if not kind or not target:
        return "", ""
    return kind, target


def _flight_source(flight_id: str) -> Tuple[dict, _Source]:
    f = _flights.get(str(flight_id or ""))
    if f is None:
        raise _flights.FlightError("unknown_flight", 404, "no such route")
    kind, target = source_of_flight(f)
    src = _find(_settings(), kind, target) if kind else None
    if src is None:
        raise _flights.FlightError(
            "no_source", 404, "this route did not come from a configured source"
        )
    return f, src


def for_flight(flight_id: str) -> dict:
    """``GET /api/flights/intake-default/for-flight/{id}``: the GET shape for
    the source that flight came from. 404 ``unknown_flight`` / ``no_source``."""
    return _dto(_flight_source(flight_id)[1])


# --------------------------------------------------------------------------- #
# Writing a source's plan field
# --------------------------------------------------------------------------- #
def _write_plan_field(src: _Source, plan_id: str) -> None:
    """Set (``""``: clear) ``src``'s plan field, the way the Intake dialog's
    saves do: the same store call, a sync stamp, the settings hooks."""
    from backend.config import settings as _st
    from backend.web.core import settings_hooks as _hooks

    st = _strict_settings()
    sig_before = _hooks.pipeline_signature()
    try:
        if src.kind == "tickets":
            i, _ = _ticket_source(st, src.target)
            if i < 0:
                raise _flights.FlightError(
                    "unknown_source", 404, "no tickets source %s" % src.target
                )
            rows = [s.to_dict() for s in st.ticketing.sources]
            if plan_id:
                rows[i]["plan"] = plan_id
            else:
                rows[i].pop("plan", None)
            _st.set_ticketing_sources(rows)
            path = "ticketing.sources"
        else:
            field = _TABLE[src.kind]
            table = copy.deepcopy(dict(getattr(st.github, field, None) or {}))
            block = dict(table.get(src.target) or {})
            if plan_id:
                block["plan"] = plan_id
            else:
                block.pop("plan", None)
            if block:
                table[src.target] = block
            else:
                table.pop(src.target, None)
            _st.update_settings(github={field: table})
            path = "github." + field
    except _st.SettingsUnreadable as err:
        raise _flights.FlightError("settings_unreadable", 409, str(err)) from err
    try:
        from backend.web.core import settings_sync

        settings_sync.local_change([path])
    except Exception:  # noqa: BLE001 — a save never fails on sync
        pass
    _hooks.after_settings_change([path], source="", pipeline_before=sig_before)


def _references(st, plan_id: str, but: _Source) -> int:
    """How many OTHER sources name ``plan_id`` as their plan."""
    n = 0
    for src in st.ticketing.sources:
        key = str(getattr(src, "id", "") or getattr(src, "provider", "") or "")
        if but.kind == "tickets" and key == but.target:
            continue
        if str(getattr(src, "plan", "") or "").strip() == plan_id:
            n += 1
    for kind, field in _TABLE.items():
        for repo, block in (getattr(st.github, field, None) or {}).items():
            if kind == but.kind and str(repo) == but.target:
                continue
            if str((block or {}).get("plan", "") or "").strip() == plan_id:
                n += 1
    return n


def _replaceable(st, src: _Source) -> Optional[dict]:
    """The source's own saved plan when it may be rewritten in place: it
    exists, is not a built-in or a depth cut, runs on no trigger of its own,
    and no other source names it."""
    pid = src.plan
    if not pid or pid in _flights.BUILTIN_IDS or _flights._CUT_RE.match(pid):
        return None
    plan = _flights.get_plan(pid)
    if plan is None or plan.get("builtin"):
        return None
    if (plan.get("trigger") or {}).get("kind") in _SELF_TRIGGERED:
        return None
    if _references(st, pid, src):
        return None
    return plan


def plan_steps(raw_steps, seeded_from: str = "") -> List[dict]:
    """Steps reduced to a plan's shape, validated strictly (a
    :class:`flights.PlanError` names the field): only the step fields a plan
    keeps (no run state). The group a grounded editor puts round the saved
    stop it was seeded from is dropped — one wrapping the WHOLE chain, or
    one labelling ``seeded_from`` (the source's default before this save) —
    since the next grounding adds it back; any other group is kept."""
    if not isinstance(raw_steps, list):
        raise _flights.PlanError("steps is a list", "steps")
    keep = (
        "id",
        "kind",
        "title",
        "config",
        "on_fail",
        "halt",
        "group",
        "when",
        "pos",
        "parallel",
    )
    rows = [
        {k: copy.deepcopy(s[k]) for k in keep if k in s} if isinstance(s, dict) else s
        for s in raw_steps
    ]
    steps = _flights.normalize_steps(rows, strict=True)
    gids = {(s.get("group") or {}).get("id") for s in steps}
    whole = len(gids) == 1 and None not in gids
    for s in steps:
        g = s.get("group")
        if g and (whole or (seeded_from and g.get("plan_id") == seeded_from)):
            s.pop("group", None)
    return steps


def _answer(src: _Source, previous: str) -> dict:
    st = _settings()
    now = _find(st, src.kind, src.target) or src
    return dict(_dto(now), previous_plan_id=previous)


def set_steps(kind, target, raw_steps, name: str = "") -> dict:
    """``PUT {kind, target, steps, name?}``: save ``steps`` as the source's
    own saved plan — its existing own one rewritten in place when nothing
    else shares it (its name kept unless ``name``), else a new plan named
    ``name`` or "<label> stops" — and point the source at it."""
    src = _need(_settings(), kind, target)
    seed = _dto(src)["plan_id"]
    return _save_steps(src, plan_steps(raw_steps, seed), str(name or "").strip())


def _save_steps(src: _Source, steps: List[dict], name: str) -> dict:
    st = _strict_settings()
    previous = src.plan
    plan = _replaceable(st, src)
    if plan is not None:
        body = dict(plan, steps=steps)
        if name:
            body["name"] = name
    else:
        body = {
            "name": name or ("%s stops" % src.label),
            "description": "The default stops for %s." % src.label,
            "trigger": {"kind": _INTAKE_KIND[src.kind]},
            "steps": steps,
        }
    saved = _flights.save_plan(body)
    if saved["id"] != previous:
        _write_plan_field(src, saved["id"])
    return _answer(src, previous)


def set_plan(kind, target, plan_id) -> dict:
    """``PUT {kind, target, plan_id}``: the source flies an existing plan
    (404 ``unknown_plan``), or ``""`` — back to our built-in."""
    src = _need(_settings(), kind, target)
    pid = str(plan_id or "").strip()
    if pid and _flights.get_plan(pid) is None:
        raise _flights.FlightError("unknown_plan", 404, "no such saved route: %s" % pid)
    previous = src.plan
    if pid != previous:
        _write_plan_field(src, pid)
    return _answer(src, previous)


def set_from_flight(flight_id) -> dict:
    """``PUT {from_flight}``: that flight's steps (as a plan's) become its
    source's default, as :func:`set_steps`. 404 ``unknown_flight`` /
    ``no_source``."""
    f, src = _flight_source(str(flight_id or ""))
    seed = _dto(src)["plan_id"]
    return _save_steps(src, plan_steps(f["steps"], seed), "")


def put(body) -> dict:
    """``PUT /api/flights/intake-default``: one of the three bodies."""
    body = body if isinstance(body, dict) else {}
    if "from_flight" in body:
        return set_from_flight(body.get("from_flight"))
    if "steps" in body:
        return set_steps(
            body.get("kind"),
            body.get("target"),
            body.get("steps"),
            body.get("name") or "",
        )
    if "plan_id" in body:
        return set_plan(body.get("kind"), body.get("target"), body.get("plan_id"))
    raise _flights.FlightError("bad_body", 400, "send steps, plan_id or from_flight")
