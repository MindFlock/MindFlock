"""``mindflock init`` — the guided first run, so nobody has to sequence it by hand.

Getting from a fresh install to a first agent session takes four things, in
order: the host dependencies (tmux, a coding-agent CLI), that CLI actually
*logged in*, a folder to work in, and a running server. The README spells them
out as prose, which leaves a stranger to work out which of them are already
true on their machine — and the one that hurts most is invisible: an agent CLI
that is installed but never authenticated starts a session whose terminal sits
on a login screen forever, looking like MindFlock hung.

Every terminal door tells the same story: THREE steps to a first agent —
install the tools, sign in to your agent, pick a project folder — rendered by
one function (:func:`steps_block`, ✓/✗ per step and a "← you are here" marker)
and closed by exactly ONE next action (:func:`next_action`). install.sh prints
it through ``mindflock init --steps``, ``mindflock init`` ends on it, and the
top of ``mindflock serve`` prints it with step 3 the boxed browser address.
"You're set." appears only when all three are done; the agent's sign-in
counts as required (the web checklist's reading, :mod:`backend.web.core.
setup_steps` ``doctor_progress``), so a signed-out agent never reads as ready.

:func:`report` is the non-interactive half: the serve banner (see
:mod:`backend.web.run`) and a piped-stdin ``init``. That call site sets the
rules — the desktop app auto-starts the server and polls its port, so run.py
prints this from a background thread that cannot delay the bind, and nothing
here prompts (nobody is watching that thread's stdin) or raises (a report that
blew up would throw a traceback across the boot banner). The swallow in
:func:`report` is the only place that degrades this text: the boot path
deliberately keeps no fallback copy of it to rot out of sync.

:func:`run` is the interactive wizard behind ``mindflock init`` (and
``mindflock serve --setup``). It reuses the doctor's own fix loop
(:func:`backend.cli._fix_checks`) limited to the ``run`` group — installing
tmux and the agent, and signing the agent in, are doctor checks that already
carry runnable commands — so nothing optional (Tailscale, GitHub, the MTU…)
is ever offered on a first run, and a second copy of that confirm-run-re-probe
dance can't drift from the first one.

"First computer, or join one you already have?" (:mod:`backend.onboarding`)
is asked BEFORE the agent sign-in, because joining brings the default agent,
the GitHub token and ticket sources along — but only when another MindFlock
answers on this tailnet (:func:`_mindflock_peer_nearby`); everyone else sees
``mindflock join`` in the block's closing line instead of a returning-user
question on screen one.

Nothing here marks a checklist step by hand. Each step is marked by the
backend hook that observes the thing itself — the doctor run this wizard
performs marks "Install the tools" and "Sign in to your agent"; the first
session create marks "Start your first agent" — so finishing the wizard
without starting anything leaves the app's own first-run surfaces in place.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, List, Optional, TextIO

if TYPE_CHECKING:
    from backend.doctor import Check

__all__ = ["report", "run"]

#: How many folders the wizard offers to choose from, and how many the
#: (much terser) first-run report lists. Long enough to contain the repo the
#: user meant, short enough to stay a glance rather than a page.
_MAX_CANDIDATES = 8
_REPORT_CANDIDATES = 5

#: Stop re-asking which folder to use after this many unusable answers, so a
#: scripted stdin that keeps answering nonsense can never trap the wizard in a
#: loop nobody is watching.
_MAX_REPO_PROMPTS = 5

#: Stand-in seed prompt for the copy-pasteable ``mindflock new`` line. A concrete
#: task reads as an example; ``-p "…"`` reads as syntax to decode.
_EXAMPLE_PROMPT = "add a test for the login flow"


# --------------------------------------------------------------------------- #
# Probes — every one of these degrades instead of raising, because both entry
# points run on paths (a server boot, a CLI a stranger just installed) where an
# exception costs far more than a missing line of advice.
# --------------------------------------------------------------------------- #
def _stdin_is_tty() -> bool:
    """True when stdin can be prompted at all.

    Mirrors the guard in :func:`backend.cli._fix_checks`: without it a piped or
    closed stdin turns every prompt into an instant EOF, and a stdin that is
    open but unattended (the desktop app's spawn) turns it into a wait with
    nothing on screen to explain what it is waiting for."""
    try:
        return bool(sys.stdin.isatty())
    except Exception:  # noqa: BLE001 — exotic stdin replacements
        return False


def _doctor_checks(group: str = "") -> List["Check"]:
    """Every doctor result (or only ``group``'s), or ``[]`` when the doctor
    itself could not run.

    The wizard reports what it can: a broken import or a probe that blows up
    costs the dependency table, not the rest of the setup."""
    try:
        from backend import doctor

        return list(doctor.run_checks(group) if group else doctor.run_checks())
    except Exception:  # noqa: BLE001 — no checks is survivable; a traceback isn't
        return []


def _remembered_repo() -> str:
    """``general.last_repo_path`` — the folder the most recent session or wizard
    run used — or ``""``. Never raises: an unreadable settings store just means
    we have no memory to rank the suggestions with."""
    try:
        from backend.config.settings import load_settings

        return str(load_settings().general.last_repo_path or "")
    except Exception:  # noqa: BLE001 — advisory only
        return ""


def _remember_repo(path: str) -> None:
    """Persist the picked folder as ``general.last_repo_path`` so the New Session
    dialog and the ``/api/repos/suggest`` ranking both start where the user just
    said they work. Never raises — a read-only settings store costs the memory,
    not the setup that earned it."""
    try:
        from backend.config import settings as settings_mod

        settings_mod.update_settings(general={"last_repo_path": path})
    except Exception:  # noqa: BLE001 — a store we can't write is not a setup failure
        pass


def _is_git_worktree(path: str) -> bool:
    """True when MindFlock will treat ``path`` as a git-backed folder.

    Asks git itself (``rev-parse --is-inside-work-tree``, via
    :mod:`backend.web.core.git_ops`) because that is the same question the
    session-create path asks (:func:`backend.web.core.plain_repo._prepare_plain_repo`
    calls the very same probe). A SUBDIRECTORY of a repo carries no ``.git`` of
    its own, so testing for that entry would tell someone standing in
    ``~/code/foo/src`` that diff, commit and PR are off — and then hand them a
    session with all three on. The probe lives under the web extras like the
    picker does, so its absence degrades to the cheap existence test rather than
    to no answer at all."""
    try:
        from backend.web.core.git_ops import _is_git_repo

        return bool(_is_git_repo(path))
    except Exception:  # noqa: BLE001 — no web extras, or git itself unlaunchable
        return os.path.exists(os.path.join(path, ".git"))


def _candidate_repos(limit: int = _MAX_CANDIDATES) -> List[dict]:
    """Folders to offer as the working repo, best first. Never raises.

    The picker backs ``GET /api/repos/suggest`` and therefore lives under
    :mod:`backend.web.core`, so it is imported lazily and its absence is
    survivable: an install without the web extras must still be able to finish
    this wizard, and "the folder you are standing in" is enough to do that."""
    remembered = _remembered_repo()
    recent = [remembered] if remembered else []
    found: List[dict] = []
    try:
        from backend.web.core.repo_picker import suggest_repos

        found = [
            dict(s)
            for s in suggest_repos(recent_paths=recent, cwd=os.getcwd(), limit=limit)
        ]
    except Exception:  # noqa: BLE001 — no web extras, or a scan that hit a bad mount
        found = []
    return found[:limit] if found else _fallback_candidates(recent)


def _fallback_candidates(recent: List[str]) -> List[dict]:
    """The suggestion list we can build without the picker: the remembered folder
    and the current directory, in the picker's own wire shape so callers never
    have to care which one they got. ``is_git`` comes from the same probe the
    picker uses, so a folder does not change its answer depending on which of the
    two lists the user is looking at."""
    out: List[dict] = []
    seen: set[str] = set()
    for raw, source in [(p, "recent") for p in recent] + [(os.getcwd(), "cwd")]:
        try:
            path = os.path.realpath(os.path.expanduser(raw))
        except Exception:  # noqa: BLE001 — an unresolvable path is simply not offered
            continue
        if path in seen or not os.path.isdir(path):
            continue
        seen.add(path)
        out.append(
            {
                "path": path,
                "name": os.path.basename(path) or path,
                "is_git": _is_git_worktree(path),
                "source": source,
            }
        )
    return out


def _git_note(candidate: dict) -> str:
    """The one-word status shown beside a candidate folder. Plain "no git yet"
    rather than silence, because a non-git folder still works — it just works
    without diff, commit and PR."""
    return "git repo" if candidate.get("is_git") else "no git yet"


# --------------------------------------------------------------------------- #
# The three steps — ONE block every terminal door prints (install.sh via
# `mindflock init --steps`, `mindflock init`, the top of `mindflock serve`) and
# the one next-action line every door (doctor included) ends on.
# --------------------------------------------------------------------------- #
#: The address the browser door is on when nothing says otherwise.
DEFAULT_URL = "http://127.0.0.1:8765"

#: An agent's name as people know it (the provider id otherwise).
_AGENT_NAMES = {
    "claude": "Claude Code",
    "codex": "Codex",
    "aider": "Aider",
    "opencode": "OpenCode",
    "antigravity": "Antigravity",
    "cline": "Cline",
    "goose": "Goose",
}

#: A tool's name on the "Install the tools" row (check id → name).
_TOOL_NAMES = {"git": "git", "tmux": "tmux", "node": "Node.js"}

#: Step titles — the web checklist's words for steps 1 and 2 (setup_steps).
STEP_TITLES = ("Install the tools", "Sign in to your agent", "Pick a project folder")

OPTIONAL_LINE = (
    "Optional, later: GitHub, ticket sources, phone. "
    "Already use MindFlock elsewhere? mindflock join"
)


def agent_name(provider: str) -> str:
    """``"Claude Code"`` for ``claude``; the provider id for anything unknown."""
    return _AGENT_NAMES.get(provider or "", provider or "your agent")


def _tool_name(c: "Check") -> str:
    if c.id == "agent-cli":
        return agent_name(c.provider)
    return _TOOL_NAMES.get(c.id, c.label)


def _plain(text: str) -> str:
    """A fix line without the backticks a web page renders as code."""
    return (text or "").replace("`", "")


def sign_in_hint(auth: Optional["Check"]) -> str:
    """How to sign the default agent in, as one plain instruction."""
    if auth is None:
        return "sign in to your agent"
    cmd = (auth.cmd or "").strip()
    if cmd and "login" in cmd:
        return "run %s" % cmd
    if cmd:
        quit_ = "type /exit" if cmd == "claude" else "quit it"
        return "run %s, sign in, then %s" % (cmd, quit_)
    return _plain(auth.fix) or "sign in from inside %s" % agent_name(auth.provider)


def readiness(checks: List["Check"], folder: str = "") -> dict:
    """Where the three steps stand, from a doctor run (``run`` group at least)
    and the project folder (``""`` = not picked yet).

    ``tools``: no ``run`` row other than the sign-in fails. ``sign_in``: the
    default agent is installed and its sign-in row is not a warning (a CLI
    with nothing to probe counts as signed in — the web checklist's reading).
    Never raises."""
    run = [c for c in checks if getattr(c, "group", "") == "run"]
    tools = [c for c in run if c.id != "agent-auth"]
    missing = [c for c in tools if c.status == "fail"]
    cli = next((c for c in run if c.id == "agent-cli"), None)
    auth = next((c for c in run if c.id == "agent-auth"), None)
    provider = (cli.provider if cli else "") or (auth.provider if auth else "")
    # A row the doctor did not return is not held against the user (every
    # real run returns both; a narrowed probe list may not).
    signed_in = (cli is None or cli.status != "fail") and (
        auth is None or auth.status not in ("warn", "fail")
    )
    return {
        "checked": bool(run),
        "tools": bool(run) and not missing,
        "missing": missing,
        "installable": [c for c in missing if c.install and (c.pkg or c.cmd)],
        "have": [c for c in tools if c.status != "fail"],
        "sign_in": signed_in,
        "auth": auth,
        "agent": agent_name(provider),
        "folder": folder,
        "folder_git": bool(folder) and _is_git_worktree(folder),
        "done": bool(run) and not missing and signed_in and bool(folder),
    }


def _names(cs: List["Check"]) -> str:
    return ", ".join(dict.fromkeys(_tool_name(c) for c in cs))


def next_action(st: dict, door: str, url: str = DEFAULT_URL) -> str:
    """The ONE line a door ends on. ``door``: ``install`` | ``init`` |
    ``doctor``. (``serve`` ends on its boxed address instead.)"""
    again = ". Then run mindflock init again." if door == "init" else ""
    if door == "install" and not st["done"]:
        return "Next: run mindflock init"
    if not st["checked"]:
        return "Next: run mindflock doctor again (the check could not run)"
    if not st["tools"]:
        n = len(st["installable"])
        if n and n == len(st["missing"]):
            return (
                "Next: mindflock doctor --fix installs the %d missing tool%s (%s)%s"
                % (
                    n,
                    "" if n == 1 else "s",
                    _names(st["missing"]),
                    again,
                )
            )
        if door == "doctor":
            return "Next: fix the ✗ lines above, then run mindflock doctor again"
        return "Next: mindflock doctor shows how to install %s%s" % (
            _names(st["missing"]),
            again,
        )
    if not st["sign_in"]:
        return "Next: sign in to %s — %s%s" % (
            st["agent"],
            sign_in_hint(st["auth"]),
            again,
        )
    if not st["folder"]:
        if door == "init":
            return "Next: run mindflock init again and type the path to your project folder"
        return "Next: run mindflock init to pick your project folder"
    return 'Next: mindflock serve, then open %s and click "+ New session"' % url


def _box(lines: List[str]) -> List[str]:
    width = max(len(s) for s in lines)
    out = ["  ┌" + "─" * (width + 2) + "┐"]
    out += ["  │ " + s.ljust(width) + " │" for s in lines]
    out.append("  └" + "─" * (width + 2) + "┘")
    return out


def steps_block(
    st: dict,
    door: str,
    *,
    url: str = DEFAULT_URL,
    suggestion: Optional[dict] = None,
) -> List[str]:
    """The three steps with ✓/✗ and a "← you are here" marker on the first one
    still to do. ``door``: ``install`` | ``init`` | ``serve``. ``serve``'s
    step 3 is the boxed browser address (the page walks through the rest);
    ``suggestion`` is the folder to propose when none is picked yet."""
    if st["done"]:
        head = "You're set."
    elif door == "serve":
        head = "Three steps to your first agent:"
    else:
        head = "MindFlock is installed. Three steps to your first agent:"
    if not st["checked"]:
        tools_note = "the check could not run — mindflock doctor has the details"
    elif st["tools"]:
        tools_note = _names(st["have"])
    else:
        tools_note = "missing " + _names(st["missing"])
    if st["sign_in"]:
        sign_note = st["agent"]
    elif not st["tools"]:
        sign_note = "after the tools"
    else:
        sign_note = sign_in_hint(st["auth"])
    if st["folder"] and not st.get("folder_git", True):
        folder_note = "%s has no git yet — run git init there, or pick a repo" % (
            st["folder"]
        )
    elif st["folder"]:
        folder_note = st["folder"]
    elif suggestion and suggestion.get("path"):
        if suggestion.get("is_git"):
            folder_note = "e.g. %s" % suggestion["path"]
        else:
            folder_note = (
                "%s has no git yet — run git init there, or pick a repo"
                % suggestion["path"]
            )
    else:
        folder_note = ""
    rows = [
        (st["tools"], STEP_TITLES[0], tools_note),
        (st["sign_in"], STEP_TITLES[1], sign_note),
        (bool(st["folder"]), STEP_TITLES[2], folder_note),
    ]
    here = next((i for i, r in enumerate(rows) if not r[0]), -1)
    width = max(len(r[1]) for r in rows)
    out = [head]
    shown = rows[:2] if door == "serve" else rows
    for i, (ok, title, note) in enumerate(shown):
        line = "  %s %d. %s  %s" % ("✓" if ok else "✗", i + 1, title.ljust(width), note)
        if i == here:
            line += "   ← you are here"
        out.append(line.rstrip())
    if door == "serve":
        out += _box(
            [
                "3. Open %s in your browser" % url,
                "   — it walks you through the rest.",
            ]
        )
    out.append("  " + OPTIONAL_LINE)
    return out


# --------------------------------------------------------------------------- #
# The devices question (backend.onboarding) — asked only when it can matter
# --------------------------------------------------------------------------- #
def _plan(checks: List["Check"]) -> Optional[dict]:
    """The devices plan for these checks, or ``None`` when it can't be built
    (only its ``devices`` step is read). Never asks GitHub who the token is
    (no network from here)."""
    try:
        from backend import onboarding

        return onboarding.build_plan(
            onboarding.collect(checks=[c.to_dict() for c in checks], check_user=False)
        )
    except Exception:  # noqa: BLE001 — the plan is advice; the wizard goes on
        return None


def _devices_step(plan: Optional[dict]) -> dict:
    return next((s for s in (plan or {}).get("steps", []) if s["id"] == "devices"), {})


#: How long the wizard waits for another computer on the tailnet to answer
#: as MindFlock before it stops asking "first computer, or join?".
_PEER_PROBE_S = 2.5


def _mindflock_peer_nearby(timeout: float = _PEER_PROBE_S) -> bool:
    """Whether another computer on this tailnet answers as MindFlock — the only
    case where "first computer, or join the one you already have?" is worth a
    first-timer's attention. Same probe as device discovery (the hello path on
    each online peer), run once with a short cap. Never raises: no Tailscale,
    no web extras, or nobody answering all read as "no"."""
    try:
        from backend.web.core import remote

        _self, peers = remote.tailscale_nodes()
        bases = [b for p in peers for b in remote._candidate_bases(p)][:32]
        hello_path = remote.HELLO_PATH
    except Exception:  # noqa: BLE001
        return False
    if not bases:
        return False
    import json
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor, wait

    def _hello(base: str) -> bool:
        try:
            with urllib.request.urlopen(base + hello_path, timeout=timeout) as resp:
                data = json.loads(resp.read(65536) or b"{}")
            return isinstance(data, dict) and data.get("app") == "mindflock"
        except Exception:  # noqa: BLE001 — refused, timeout, TLS, not JSON
            return False

    pool = ThreadPoolExecutor(max_workers=min(16, len(bases)))
    try:
        futures = [pool.submit(_hello, b) for b in bases]
        done, _pending = wait(futures, timeout=timeout + 0.5)
        return any(f.result() for f in done)
    except Exception:  # noqa: BLE001
        return False
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _should_ask_devices(plan: Optional[dict]) -> bool:
    """Ask "first computer, or join?" only while it is unanswered AND another
    MindFlock is on this tailnet; everyone else gets ``mindflock join`` in the
    closing lines instead of a returning-user question on screen one."""
    step = _devices_step(plan)
    if step.get("status") != "todo" or not step.get("ask"):
        return False
    return _mindflock_peer_nearby()


def _ask_devices(plan: Optional[dict]) -> str:
    """Ask Setup's devices question when it hasn't been answered; store the
    answer (``"first"``/``"join"``) and return it — ``""`` when not asked."""
    step = _devices_step(plan)
    if step.get("status") != "todo" or not step.get("ask"):
        return ""
    print()
    print("Another computer on your tailnet runs MindFlock.")
    print(
        "Is this your first computer with MindFlock, or do you already use it on that one?"
    )
    print("  (joining it first brings your settings, GitHub token and ticket sources)")
    try:
        answer = (
            input("\n  [F]irst computer / [j]oin the one I already have: ")
            .strip()
            .lower()
        )
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    choice = "join" if answer.startswith("j") else "first"
    try:
        from backend import onboarding

        onboarding.set_choice(choice)
    except Exception:  # noqa: BLE001 — an unwritable store costs the memory only
        pass
    if choice == "join":
        print()
        print(
            "  On your other computer: Settings → Your devices → Add a device (or run"
        )
        print("  mindflock devices bootstrap there). It shows a code; then, here:")
        print("    mindflock devices join DEVICE CODE")
    return choice


# --------------------------------------------------------------------------- #
# Read-only doors: the serve banner, piped-stdin init, install.sh
# --------------------------------------------------------------------------- #
def _known_folder() -> str:
    """The project folder already picked (``general.last_repo_path``) when it
    still exists, else ``""``."""
    path = _remembered_repo()
    return path if path and os.path.isdir(path) else ""


def report(
    stream: Optional[TextIO] = None, *, serving: bool = False, url: str = DEFAULT_URL
) -> Optional[dict]:
    """Print the three steps for a door that cannot ask anything, and return
    where they stand (``None`` when even that failed).

    ``serving``: the top of ``mindflock serve`` — the steps with the boxed
    browser address as step 3 (or, once all three are done, just the address);
    the doctor runs only its ``run`` probes there. Otherwise the piped-stdin
    ``mindflock init`` summary, ending on its one next action.

    Never prompts, never blocks, never raises — see the module docstring for why
    the server boot path cannot tolerate any of the three. ``stream`` is
    resolved at call time so a caller that swapped ``sys.stdout`` is honored.
    """
    out = stream if stream is not None else sys.stdout
    try:
        return _print_report(out, serving=serving, url=url)
    except Exception:  # noqa: BLE001 — advisory text must never break its caller
        return None


def _print_report(out: TextIO, *, serving: bool, url: str) -> dict:
    checks = _doctor_checks("run" if serving else "")
    folder = _known_folder()
    st = readiness(checks, folder)
    if serving:
        if st["done"]:
            lines = _box(["Open %s in your browser" % url])
        else:
            lines = steps_block(st, "serve", url=url)
    else:
        cands = [] if folder else _candidate_repos(_REPORT_CANDIDATES)
        lines = steps_block(st, "init", suggestion=cands[0] if cands else None)
        lines += ["", next_action(st, "init", url)]
    print("\n".join(lines), file=out)
    return st


def print_steps(door: str = "install", stream: Optional[TextIO] = None) -> int:
    """``mindflock init --steps``: the block + next line for ``door`` (what
    install.sh ends on). Read-only; always exits 0."""
    out = stream if stream is not None else sys.stdout
    try:
        st = readiness(_doctor_checks(), _known_folder())
        lines = steps_block(st, door) + ["", next_action(st, door)]
        print("\n".join(lines), file=out)
    except Exception:  # noqa: BLE001 — an installer's last lines must not crash
        print("Next: run mindflock init", file=out)
    return 0


# --------------------------------------------------------------------------- #
# Interactive wizard (`mindflock init`, `mindflock serve --setup`)
# --------------------------------------------------------------------------- #
def run(assume_yes: bool = False, *, serving: bool = False) -> int:
    """Walk the user through the three steps; return a process exit code.

    Step 1 and 2 reuse the doctor's own fix loop (:func:`backend.cli._fix_checks`)
    limited to the ``run`` group — installing tmux and the agent, and signing
    the agent in, are doctor checks carrying runnable commands — so nothing
    optional (Tailscale, GitHub, the MTU…) is ever offered here. Step 3 picks
    the project folder (remembered for next time). It ends on the three-steps
    block and ONE next action; "You're set." only when all three are done.

    Exit code 0 when no required dependency fails, 1 otherwise, matching
    ``mindflock doctor``: that makes ``mindflock init && mindflock serve`` stop
    at the honest place.

    ``assume_yes`` takes every default without asking, for scripts and tests.
    It deliberately does *not* accept the fix offers: shelling out to package
    installers nobody is watching is not a default anyone should take by
    accident. A non-TTY stdin degrades to :func:`report`, explanation first.

    ``serving`` says the calling process is the server (``mindflock serve
    --setup``): the block and the next action are then left to the serve
    banner printed the moment this call returns.
    """
    from backend import cli

    if not assume_yes and not _stdin_is_tty():
        # Piped or closed stdin (an installer script, CI, the desktop app's
        # spawn): every prompt below would come straight back as EOF, so say
        # so FIRST, then print the same facts.
        print(
            "No terminal attached, so this is a read-only summary — run "
            "mindflock init in a terminal to be walked through it."
        )
        print()
        report(serving=serving)
        return 0

    print("MindFlock setup — three steps to your first agent.")
    checks = _doctor_checks()
    if not checks:
        print("  (the check could not run — mindflock doctor has the details)")

    plan = _plan(checks)
    if not assume_yes and _should_ask_devices(plan):
        _ask_devices(plan)
        plan = _plan(checks)
    joining = _devices_step(plan).get("choice") == "join" and (
        _devices_step(plan).get("status") != "ok"
    )
    if not assume_yes:
        if joining:
            # Sign the agent in after joining: the join may change which
            # agent is the default here.
            later = [c for c in checks if c.id.startswith("agent-auth")]
            checks = (
                cli._fix_checks(
                    [c for c in checks if not c.id.startswith("agent-auth")],
                    groups=("run",),
                )
                + later
            )
        else:
            checks = cli._fix_checks(checks, groups=("run",))

    print()
    repo = _pick_repo(_candidate_repos(), assume_yes)
    if repo:
        _remember_repo(repo)

    failed = [c for c in checks if c.status == "fail"]
    if not serving:
        st = readiness(checks, repo)
        print()
        print("\n".join(steps_block(st, "init")))
        print()
        print(next_action(st, "init"))
    return 1 if failed else 0


def _pick_repo(candidates: List[dict], assume_yes: bool = False) -> str:
    """Ask which folder MindFlock should work in; return an absolute path.

    A number picks from the printed list, anything else is read as a path, and an
    empty answer keeps the top suggestion — which is the folder the last session
    used, so the common case is one keystroke. Returns ``""`` only when there was
    nothing to suggest and nobody answered with a path."""
    top = candidates[0]["path"] if candidates else ""
    if candidates:
        print("Which project folder should MindFlock work in?")
        print()
        width = max(len(c["path"]) for c in candidates)
        for n, c in enumerate(candidates, 1):
            note = _git_note(c)
            if c.get("source") == "recent":
                note += ", used most recently"
            print("  %d  %s  %s" % (n, c["path"].ljust(width), note))
    else:
        print("I could not find a folder to suggest.")
    if assume_yes:
        return top

    # Leading newline in the prompt, like the doctor's fix loop: it spaces the
    # question off the list without leaving a stray blank line behind when
    # there is no question to ask.
    prompt = (
        "\n  Enter = 1, or type a number or a path: "
        if candidates
        else "\n  Type the path to your project folder: "
    )
    for _ in range(_MAX_REPO_PROMPTS):
        try:
            answer = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()  # leave the shell prompt on a line of its own
            return top
        if not answer:
            return top
        if answer.isdigit() and candidates:
            n = int(answer)
            if 1 <= n <= len(candidates):
                return candidates[n - 1]["path"]
            print(
                "  there is no %d in that list — pick 1-%d, or type a path"
                % (n, len(candidates))
            )
            continue
        path = os.path.realpath(os.path.expanduser(answer))
        if os.path.isdir(path):
            return path
        print(
            "  %s is not a folder yet — create it first, or pick from the list" % path
        )
    return top
