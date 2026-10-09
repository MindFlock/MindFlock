"""The first-run plan: one ordered list of steps every setup surface renders.

There used to be three first-run wizards — ``mindflock init``
(:mod:`backend.init_wizard`), Setup in the web UI (``SetupDialog.tsx``) and
the desktop app's offline page — each with its own idea of what comes next,
and none of them covering your other computers, Tailscale or GitHub. Joining
another computer came LAST in Setup, after the agent and GitHub steps, though
joining is what brings the default agent, the GitHub token and the ticket
sources along. This module is the one answer now: ``GET /api/onboarding``
serves it, Setup draws it, ``mindflock init`` prints it.

**The order** (:data:`ORDER`)::

    deps → devices ("First computer, or join one you already have?")
         → agent sign-in → tailscale (only when going multi-device)
         → github → repo

Each step is ``ok`` / ``todo`` / ``skip`` with a one-line ``reason``, so a
surface never has to work out why a step doesn't apply. What joining changes:
the agent and GitHub steps are ``skip`` while a join is still to come —
joining brings the GitHub token (settings sync shares it) and the default
agent, though NOT the agent's sign-in: accounts stay on each computer
(``auth_profiles`` is LOCAL in :mod:`backend.web.core.settings_sync`), so the
agent step comes back as ``todo`` once joined if this computer isn't signed
in.

Two halves, for testing: :func:`collect` gathers the facts (doctor checks,
fleet membership, Tailscale health, the GitHub sign-in — slow, never raises),
and :func:`build_plan` is a pure function of them.
"""

from __future__ import annotations

from typing import Callable, List, Optional

__all__ = ["ORDER", "build_plan", "collect", "plan"]

ORDER = ("deps", "devices", "agent", "tailscale", "github", "repo")

TITLES = {
    "deps": "Dependencies",
    "devices": "First computer, or join one you already have?",
    "agent": "Sign in to your agent",
    "tailscale": "Tailscale",
    "github": "Connect GitHub",
    "repo": "Your first repo",
}


def _step(sid: str, status: str, reason: str, **extra) -> dict:
    return {
        "id": sid,
        "title": TITLES[sid],
        "status": status,
        "reason": reason,
        **extra,
    }


def _joined(dev: dict) -> bool:
    return bool(dev.get("in_fleet")) and int(dev.get("members") or 0) >= 2


def _joining(dev: dict) -> bool:
    """A join is still to come: chosen, or under way, and not done."""
    if _joined(dev):
        return False
    return dev.get("choice") == "join" or dev.get("join_state") in (
        "waiting",
        "joining",
    )


def _deps(checks: List[dict]) -> dict:
    failing = [c for c in checks if c.get("status") == "fail"]
    if failing:
        names = ", ".join(c.get("label") or c.get("id") or "?" for c in failing)
        return _step(
            "deps",
            "todo",
            "missing: " + names,
            missing=[c.get("id") for c in failing],
            cli="mindflock doctor --fix",
        )
    if not checks:
        return _step(
            "deps", "todo", "the dependency check couldn't run", cli="mindflock doctor"
        )
    return _step("deps", "ok", "everything required is installed")


def _devices(dev: dict, ts_ready: bool) -> dict:
    choice = dev.get("choice") or ""
    if _joined(dev):
        n = int(dev["members"]) - 1
        return _step(
            "devices",
            "ok",
            "joined with %d other computer%s" % (n, "" if n == 1 else "s"),
            choice="join",
        )
    state = dev.get("join_state") or ""
    if state in ("waiting", "joining"):
        who = dev.get("join_host") or "your other computer"
        return _step(
            "devices",
            "todo",
            (
                "waiting for %s to approve" % who
                if state == "waiting"
                else "joining %s…" % who
            ),
            choice="join",
        )
    if choice == "first":
        return _step(
            "devices",
            "ok",
            "this is your first computer — add others later (Settings → Devices)",
            choice="first",
        )
    if choice == "join":
        reason = "paste the code your other computer shows (Settings → Devices → Add a device)"
        if not ts_ready:
            reason += " — after Tailscale is signed in here (below)"
        return _step(
            "devices",
            "todo",
            reason,
            choice="join",
            cli="mindflock devices join DEVICE CODE",
        )
    return _step(
        "devices",
        "todo",
        "joining first brings your settings, GitHub token and ticket sources from it",
        choice="",
        ask=True,
    )


def _agent(checks: List[dict], dev: dict) -> dict:
    by_id = {c.get("id"): c for c in checks}
    cli = by_id.get("agent-cli") or {}
    auth = by_id.get("agent-auth") or {}
    provider = auth.get("provider") or cli.get("provider") or ""
    if _joining(dev):
        return _step(
            "agent",
            "skip",
            "joining brings your default agent; sign in here after "
            "(sign-ins stay on each computer)",
            provider=provider,
        )
    if cli.get("status") == "fail":
        return _step(
            "agent",
            "todo",
            "install %s first (Dependencies)" % (provider or "the agent CLI"),
            provider=provider,
            cli="mindflock doctor --fix",
        )
    st = auth.get("status")
    if st == "ok":
        return _step("agent", "ok", "signed in to %s" % provider, provider=provider)
    if st == "warn":
        return _step(
            "agent",
            "todo",
            auth.get("detail") or "no sign of a login was found",
            provider=provider,
            # A declared login flow: the UI offers "Sign in to <agent>".
            sign_in=bool(auth.get("cmd")),
            cli=auth.get("cmd") or auth.get("fix") or "",
        )
    return _step(
        "agent",
        "ok",
        auth.get("detail") or "no sign-in to check for %s" % (provider or "this agent"),
        provider=provider,
    )


def _tailscale_ready(ts: dict) -> bool:
    if not ts.get("installed") or ts.get("backend_state") != "Running":
        return False
    return not any(i.get("level") == "fail" for i in ts.get("issues") or [])


def _tailscale(ts: dict, dev: dict) -> dict:
    if not (_joined(dev) or _joining(dev)):
        return _step("tailscale", "skip", "only needed to connect your other computers")
    issues = [i for i in ts.get("issues") or [] if i.get("level") in ("fail", "warn")]
    if _tailscale_ready(ts):
        warn = next((i for i in issues if i.get("id") == "key_expiry"), None)
        who = ts.get("tailnet") or ""
        if warn:
            return _step(
                "tailscale", "todo", warn.get("message", ""), fix=warn.get("fix", "")
            )
        return _step("tailscale", "ok", "signed in" + (" to " + who if who else ""))
    first = (issues or ts.get("issues") or [{}])[0]
    return _step(
        "tailscale",
        "todo",
        first.get("message") or "Tailscale isn't running here",
        fix=first.get("fix", ""),
        issue=first.get("id", ""),
        cli=first.get("fix", "") or "tailscale up",
    )


def _github(gh: dict, dev: dict) -> dict:
    if not gh.get("connected") and _joining(dev):
        return _step(
            "github",
            "skip",
            "comes from your other computer when you join (settings sync shares the token)",
        )
    if not gh.get("connected"):
        return _step(
            "github",
            "todo",
            "connect GitHub to open PRs and push (skip it for other forges)",
            cli="gh auth login --web  (or make a token: %s)" % gh.get("token_url", ""),
        )
    who = "@" + gh["login"] if gh.get("login") else "your GitHub token"
    ident = gh.get("identity") or {}
    if not (ident.get("name") and ident.get("email")):
        return _step(
            "github",
            "todo",
            "connected as %s — now tell git your name and email" % who,
            identity=False,
            cli='git config --global user.name "Your Name" && '
            "git config --global user.email you@example.com",
        )
    return _step("github", "ok", "connected as %s" % who, identity=True)


def _repo(repo: dict) -> dict:
    if repo.get("path"):
        return _step("repo", "ok", "working in %s" % repo["path"], path=repo["path"])
    if repo.get("onboarded"):
        return _step("repo", "ok", "you've started a session")
    return _step(
        "repo",
        "todo",
        "pick a folder and start your first session",
        cli='mindflock new /path/to/repo -p "…"',
    )


def build_plan(facts: dict) -> dict:
    """The plan from ``facts`` (see :func:`collect` for the shape). Pure."""
    checks = list(facts.get("checks") or [])
    dev = dict(facts.get("devices") or {})
    ts = dict(facts.get("tailscale") or {})
    steps = [
        _deps(checks),
        _devices(dev, _tailscale_ready(ts)),
        _agent(checks, dev),
        _tailscale(ts, dev),
        _github(dict(facts.get("github") or {}), dev),
        _repo(dict(facts.get("repo") or {})),
    ]
    todo = [s["id"] for s in steps if s["status"] == "todo"]
    nxt = todo[0] if todo else ""
    # Joining needs Tailscale up on this computer first, though the question
    # is asked above it: point at Tailscale while that is what blocks it.
    if nxt == "devices" and dev.get("choice") == "join" and "tailscale" in todo:
        nxt = "tailscale"
    return {"steps": steps, "next": nxt, "done": not todo, "os": facts.get("os", "")}


# --------------------------------------------------------------------------- #
# facts
# --------------------------------------------------------------------------- #
def _safe(fn: Callable, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001 — a fact we can't read is a fact unknown
        return default


def _checks() -> List[dict]:
    from backend import doctor

    return [c.to_dict() for c in doctor.run_checks()]


def _devices_facts() -> dict:
    from backend.config.settings import load_settings
    from backend.web.core import fleet

    out = {
        "choice": _safe(lambda: load_settings().general.setup_devices, ""),
        "in_fleet": _safe(fleet.in_fleet, False),
        "members": _safe(lambda: len(fleet.live_members()), 0),
    }
    join = _safe(fleet.join_status, {}) or {}
    out["join_state"] = join.get("state") or ""
    out["join_host"] = join.get("host") or join.get("device") or ""
    return out


def _tailscale_facts() -> dict:
    from backend import tailscale_cli

    h = tailscale_cli.health()
    return {
        "installed": h.get("installed"),
        "backend_state": h.get("backend_state"),
        "tailnet": h.get("tailnet"),
        "issues": h.get("issues") or [],
    }


def _github_facts(check_user: bool) -> dict:
    from backend.web.core import github_auth

    st = github_auth.status(check_user=check_user)
    return {
        "connected": st["connected"],
        "login": st["login"],
        "identity": st["identity"],
        "token_url": st["token_url"],
    }


def _repo_facts() -> dict:
    from backend.config.settings import load_settings

    g = load_settings().general
    return {"path": g.last_repo_path or "", "onboarded": bool(g.onboarded)}


def collect(*, checks: Optional[List[dict]] = None, check_user: bool = True) -> dict:
    """Read every fact the plan needs. Never raises: an unreadable one reads
    as unknown (``{}``), which the plan renders as a step still to do."""
    from backend import osenv

    return {
        "os": _safe(osenv.os_kind, ""),
        "checks": checks if checks is not None else _safe(_checks, []),
        "devices": _safe(_devices_facts, {}),
        "tailscale": _safe(_tailscale_facts, {}),
        "github": _safe(lambda: _github_facts(check_user), {}),
        "repo": _safe(_repo_facts, {}),
    }


def plan(**kw) -> dict:
    return build_plan(collect(**kw))


def set_choice(choice: str) -> str:
    """Store Setup's devices answer (``"first"``/``"join"``, ``""`` to ask
    again) — LOCAL to this computer."""
    choice = (choice or "").strip().lower()
    if choice not in ("", "first", "join"):
        raise ValueError("choice must be 'first' or 'join'")
    from backend.config import settings as settings_store

    settings_store.update_settings(general={"setup_devices": choice})
    return choice
