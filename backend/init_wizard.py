"""``mindflock init`` — the guided first run, so nobody has to sequence it by hand.

Getting from a fresh install to a first agent session takes four things, in
order: the host dependencies (tmux, a coding-agent CLI), that CLI actually
*logged in*, a folder to work in, and a running server. The README spells them
out as prose, which leaves a stranger to work out which of them are already
true on their machine — and the one that hurts most is invisible: an agent CLI
that is installed but never authenticated starts a session whose terminal sits
on a login screen forever, looking like MindFlock hung.

Two entry points, and the split between them is the point of this module:

:func:`report` is non-interactive. It is what a first ``mindflock serve``
prints (see :mod:`backend.web.run`): the user's *actual* missing dependencies
with their one-line fixes, the folders we can see, and the exact next commands.
That call site sets the rules — the desktop app auto-starts the server and polls
its port, so run.py prints this from a background thread that cannot delay the
bind, and nothing here prompts (nobody is watching that thread's stdin) or raises
(a report that blew up would throw a traceback across the boot banner). The
swallow in :func:`report` is the only place that degrades this text: the boot path
deliberately keeps no fallback copy of it to rot out of sync.

:func:`run` is the interactive wizard behind ``mindflock init`` (and
``mindflock serve --setup``). It reuses the doctor's own fix loop
(:func:`backend.cli._fix_checks`) instead of re-implementing "install tmux",
"install the agent CLI" and "log the agent CLI in": each of those is a doctor
check that already carries a runnable command, and a second copy of that
confirm-run-re-probe dance would drift from the first one the day a fix line
changes.

Nothing here sets ``general.onboarded``. In this codebase that flag means "has
created a session" and gates the desktop app's own first-run surfaces; setting
it at the end of a wizard would silently suppress the welcome tour for someone
who has not started anything yet.
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


def _doctor_checks() -> List["Check"]:
    """Every doctor result, or ``[]`` when the doctor itself could not run.

    The wizard reports what it can: a broken import or a probe that blows up
    costs the dependency table, not the rest of the setup."""
    try:
        from backend import doctor

        return list(doctor.run_checks())
    except Exception:  # noqa: BLE001 — no checks is survivable; a traceback isn't
        return []


def _needs_attention(checks: List["Check"]) -> List["Check"]:
    """The ``warn``/``fail`` checks — the only ones a first-run summary should
    spend lines on. An ``ok`` or ``info`` check is precisely what the user does
    not need to read about."""
    return [c for c in checks if c.status in ("warn", "fail")]


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
# Non-interactive report (the first-serve banner)
# --------------------------------------------------------------------------- #
def report(stream: Optional[TextIO] = None, *, serving: bool = False) -> None:
    """Print a first-run summary: what this machine is missing, which folders it
    could work in, and the commands that come next.

    Never prompts, never blocks, never raises — see the module docstring for why
    the server boot path cannot tolerate any of the three.

    ``stream`` is resolved at call time rather than bound as a default so a
    caller that swapped ``sys.stdout`` (a test capture, the desktop app's log
    pipe) is honored. ``serving`` drops the ``mindflock serve`` line: this text
    is printed *by* the server that line would start, and telling someone to
    start what they just started is how a first-run hint loses its credibility.
    """
    out = stream if stream is not None else sys.stdout
    try:
        _print_report(out, serving=serving)
    except Exception:  # noqa: BLE001 — advisory text must never break its caller
        pass


def _print_report(out: TextIO, *, serving: bool) -> None:
    from backend import cli

    print("First run — here is where this machine actually stands:", file=out)
    print("", file=out)
    checks = _doctor_checks()
    attention = _needs_attention(checks)
    if attention:
        cli.print_checks(attention, stream=out)
        print("", file=out)
        print(
            "  `mindflock init` walks through those and can run each fix for you.",
            file=out,
        )
    elif checks:
        print(
            "  Dependencies look good — `mindflock doctor` prints the full list.",
            file=out,
        )
    else:
        print(
            "  Dependency check unavailable — `mindflock doctor` has the details.",
            file=out,
        )

    candidates = _candidate_repos(_REPORT_CANDIDATES)
    if candidates:
        print("", file=out)
        print("  Folders you could work in:", file=out)
        width = max(len(c["path"]) for c in candidates)
        for c in candidates:
            print("    %s  %s" % (c["path"].ljust(width), _git_note(c)), file=out)

    target = candidates[0]["path"] if candidates else os.getcwd()
    print("", file=out)
    print("  Next:", file=out)
    print(
        "    mindflock init          # guided setup: dependencies, agent login, your repo",
        file=out,
    )
    if not serving:
        print(
            "    mindflock serve         # start the server (the desktop app starts one for you)",
            file=out,
        )
    print('    mindflock new %s -p "%s"' % (target, _EXAMPLE_PROMPT), file=out)
    print(
        "      (a session = an isolated git worktree + a tmux session running your agent)",
        file=out,
    )
    print(
        '  …or open the MindFlock desktop app and click "+ New" — same session, from the GUI.',
        file=out,
    )


# --------------------------------------------------------------------------- #
# Interactive wizard (`mindflock init`, `mindflock serve --setup`)
# --------------------------------------------------------------------------- #
def run(assume_yes: bool = False, *, serving: bool = False) -> int:
    """Walk the user through first-run setup; return a process exit code.

    Four steps, each one skippable: print the doctor's checks, offer to run the
    fix command for every check that carries one, pick the folder to work in
    (remembered for next time), and print the commands that start the first
    session. The single :func:`backend.cli._fix_checks` call is also how tmux
    gets installed and how the agent CLI gets logged in — both are doctor checks
    carrying runnable commands — so none of that is hand-rolled here.

    Exit code 0 when setup finished, 1 when a required dependency is still
    missing, matching ``mindflock doctor``: that makes
    ``mindflock init && mindflock serve`` stop at the honest place.

    ``assume_yes`` takes every default without asking, for scripts and for our
    own tests. It deliberately does *not* accept the fix offers: shelling out to
    package installers nobody is watching is not a default anyone should be able
    to take by accident, so under ``--yes`` the fix lines are printed and the
    wizard moves on. A non-TTY stdin degrades to :func:`report` rather than
    prompting into the void.

    ``serving`` says the calling process is the server (``mindflock serve
    --setup``, see :mod:`backend.web.run`) and is threaded through to
    :func:`report` and the closing commands: both of them otherwise end on
    "``mindflock serve`` — start the server", printed by the process that binds
    the port the moment this call returns.
    """
    from backend import cli

    if not assume_yes and not _stdin_is_tty():
        # Piped or closed stdin (an installer script, CI, the desktop app's
        # spawn): every prompt below would come straight back as EOF, so print
        # the same facts and name the door instead of pretending to ask.
        report(serving=serving)
        print()
        print(
            "That is the non-interactive summary — run `mindflock init` in a "
            "terminal to be walked through it."
        )
        return 0

    print("MindFlock setup")
    print()
    print("What this machine has:")
    checks = _doctor_checks()
    if checks:
        cli.print_checks(checks)
    else:
        print(
            "  (the dependency check could not run — `mindflock doctor` has the details)"
        )

    if not assume_yes:
        checks = cli._fix_checks(checks)
    elif _needs_attention(checks):
        print()
        print(
            "  --yes never runs an installer unwatched — the fix lines above are the list."
        )

    print()
    repo = _pick_repo(_candidate_repos(), assume_yes)
    if repo:
        _remember_repo(repo)
        print()
        print("Working folder: %s" % repo)
        if not _is_git_worktree(repo):
            # Inform, never silently degrade: a session here still runs, it just
            # runs without the git-backed half of MindFlock.
            print("  Not a git repo, so diff, commit and PR are off. `git init` here —")
            print('  or tick "Create a git repo in this folder" when you create the')
            print("  session — turns them on.")
    else:
        print()
        print("No folder picked — name one when you start: mindflock new /path/to/repo")

    failed = [c for c in checks if c.status == "fail"]
    print()
    if serving:
        # This process binds the port as soon as we return, so the only command
        # left is the one that starts a session.
        print("You're set. One command:")
    else:
        print("You're set. Two commands:")
        print("  mindflock serve")
    print('  mindflock new %s -p "%s"' % (repo or "/path/to/repo", _EXAMPLE_PROMPT))
    print(
        '  (or open the MindFlock desktop app — it starts the server itself, and "+ New"'
        " does the same thing)"
    )
    if failed:
        print()
        print(
            "%d required dependenc%s still missing — the fix lines above are what's left."
            % (len(failed), "y" if len(failed) == 1 else "ies")
        )
        return 1
    return 0


def _pick_repo(candidates: List[dict], assume_yes: bool = False) -> str:
    """Ask which folder MindFlock should work in; return an absolute path.

    A number picks from the printed list, anything else is read as a path, and an
    empty answer keeps the top suggestion — which is the folder the last session
    used, so the common case is one keystroke. Returns ``""`` only when there was
    nothing to suggest and nobody answered with a path."""
    top = candidates[0]["path"] if candidates else ""
    if candidates:
        print("Which folder should MindFlock work in?")
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
        else "\n  Type the path to your repo: "
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
