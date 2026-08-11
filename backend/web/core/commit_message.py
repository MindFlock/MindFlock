"""Model-written commit messages — the ✨ button, and the autopilot's commits.

Every commit MindFlock makes needs a subject, and the two ways of getting one
were both bad. The dialog gave you an empty box (you write it, having just read
someone else's diff), and a fast-track press with nothing typed fell back to
``_autopilot_default_message`` — "Work on ft-session", a sentence that describes
the session rather than the change. The diff is right there and a model is
already installed and authenticated; asking it is strictly better than either.

How it gets a model: the session's own coding CLI, run headlessly through
:meth:`CodingProvider.oneshot_argv` — ``claude -p``, ``codex exec``, ``agy
--print``. No API key to configure, no second provider to authenticate, and the
answer comes from whatever the user already pays for. A CLI with no text-only
mode (aider) raises :class:`CommitMessageError`, which is the whole contract
here: **every caller must have a no-model fallback**. A commit that cannot be
described must still be committable.

The run is deliberately unlike a session launch: no tmux, no PTY, one process
with a timeout, stdin closed, and no skip-permissions flag — a question about the
tree has no business editing it. It runs IN the worktree, which is both what
these CLIs expect (``codex exec`` refuses to run outside a repo without being
told) and where the project's own conventions live, so a repo whose AGENTS.md
dictates a commit style gets that style.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Optional, Sequence

#: git's empty tree — the diff base for a repo whose HEAD is still unborn, so a
#: first-ever commit gets a written message like every other one. The same
#: well-known constant in every git install (``git hash-object -t tree
#: /dev/null``); the secret scanner only sees 40 hex chars.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # pragma: allowlist secret

#: How much patch text to send. Enough that a normal session's work arrives
#: whole; small enough to stay a cheap, fast call on the pathological "the agent
#: reformatted 400 files" tree, where the file list carries the meaning anyway.
DIFF_BUDGET = 24_000
#: Cap on the file-summary block (`--stat`), which is what survives truncation.
STAT_BUDGET = 4_000
#: Ceiling on what we hand back. A commit message is a paragraph; anything
#: longer is a CLI that answered with an essay, and the excess is chatter.
MESSAGE_MAX = 4_000

#: The ✨ button's budget. The user is watching a spinner they asked for, and a
#: cold CLI start plus a real model turn is ~10s — far past any HTTP default, so
#: this is generous on purpose.
TIMEOUT_INTERACTIVE = 120.0
#: The autopilot's budget. Shorter because the driver steps armed sessions ONE
#: AT A TIME: every second spent here is a second the other chains wait. A
#: timeout is not a failure of the commit, only of its subject.
TIMEOUT_AUTOPILOT = 45.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_FENCE_RE = re.compile(r"^\s*```[^\n]*\n(.*?)\n\s*```\s*$", re.S)
#: The delimiter :func:`build_prompt` asks for. CAUGHT IN THE WILD: told to reply
#: with only the message, a CLI still opened with "The staged change adds
#: retry-with-backoff, so the message should describe that behavior." — which then
#: became the commit subject. No amount of prompt sternness makes free-form output
#: safe to slice; a delimiter does, and its absence is what the heuristics below
#: are for.
_TAGGED_RE = re.compile(r"<commit>(.*?)</commit>", re.S | re.I)
_STRAY_TAG_RE = re.compile(r"</?commit>", re.I)
#: Attribution the user does not want in their history (this repo's own policy)
#: and that some CLIs add unasked.
_TRAILER_RE = re.compile(
    r"^\s*(?:co-authored-by|generated (?:with|by)|🤖 generated)\b", re.I
)
#: Lines a chatty CLI wraps the answer in ("Here's a commit message:"). Anchored
#: at both ends on purpose: an unanchored "here'?s.*" also matched a one-line
#: answer that merely STARTED that way ("Sure! Add retries"), deleting the
#: message itself and leaving nothing to commit.
_PREAMBLE_RE = re.compile(
    r"^\s*(?:(?:sure|okay|ok)[,!.]?\s*)?"
    r"(?:here(?:'s| is)[^\n:]*|(?:the |a )?commit message)?:?\s*$",
    re.I,
)


class CommitMessageError(RuntimeError):
    """No message could be generated. The sentence is shown to the user."""


def _git_out(worktree: str, *args: str, timeout: float = 30.0) -> str:
    cp = subprocess.run(
        ["git", "-C", worktree, "--no-pager", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )
    if cp.returncode != 0:
        return ""
    return cp.stdout.decode("utf-8", "replace")


def diff_base(worktree: str) -> str:
    """``HEAD``, or the empty tree when there are no commits yet."""
    cp = subprocess.run(
        ["git", "-C", worktree, "rev-parse", "--verify", "-q", "HEAD"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        timeout=15,
    )
    return "HEAD" if cp.returncode == 0 else _EMPTY_TREE


def collect_diff(worktree: str, budget: int = DIFF_BUDGET) -> tuple[str, str]:
    """``(stat, patch)`` for everything the next commit will contain.

    Baseline is HEAD, not the branch's fork point: this describes THIS commit,
    and a session's second commit must not be handed the first one's changes.

    ``add -N .`` first, so a session whose whole contribution is new files is not
    described as an empty diff — the same intent-to-add trick the Diff tab uses,
    and harmless before a commit that stages everything anyway.
    """
    subprocess.run(
        ["git", "-C", worktree, "add", "-N", "."],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    base = diff_base(worktree)
    stat = _git_out(worktree, "diff", "--stat", base)[:STAT_BUDGET]
    patch = _git_out(worktree, "diff", base)
    if len(patch) > budget:
        patch = (
            patch[:budget]
            + "\n… diff truncated — the file summary above is complete.\n"
        )
    return stat.strip(), patch.strip()


def build_prompt(stat: str, patch: str, branch: str = "", hint: str = "") -> str:
    """The whole instruction, as one argv token.

    Written to be answerable in one turn with no tool calls: everything the model
    needs is in the text, so the CLI never has to read a file (which, with
    permissions unskipped, it would be refused anyway).
    """
    parts = [
        "Write the git commit message for the staged work below.",
        "",
        "Rules:",
        "- Put the message, and nothing else, between <commit> and </commit> tags.",
        "- First line: imperative mood, under 72 characters, no trailing period.",
        "- Then a blank line and a short body ONLY if the change needs explaining;"
        " omit the body for a small or self-evident change.",
        "- Describe what the change does and why, not which files moved.",
        "- No attribution or Co-authored-by trailers, no code fences.",
    ]
    if branch:
        parts += ["", "Branch: %s" % branch]
    if hint:
        # The work's own name (a ticket title, the message already typed into the
        # dialog). Context, not an instruction to copy — a stale hint must not
        # outrank a diff that says otherwise.
        parts += ["Context for this work (may be stale): %s" % hint]
    if stat:
        parts += ["", "Files changed:", stat]
    parts += ["", "Diff:", patch or "(no textual diff — see the file summary)"]
    return "\n".join(parts)


def clean_message(raw: str) -> str:
    """A commit message from a CLI's stdout, or ``""``.

    Prefers the ``<commit>`` block :func:`build_prompt` asks for, and falls back
    to heuristics when a CLI ignores it — because CLIs are chatty in ways a commit
    message cannot survive: ANSI colour, a fenced block, a "Here's a commit
    message:" lead-in, a co-author trailer. All of it is stripped here rather than
    trusted to the prompt, because the failure mode is a literal
    ``\\`\\`\\`\\`` in the user's git history.
    """
    text = _ANSI_RE.sub("", raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    # The delimited answer, when there is one — the LAST block, because a CLI that
    # echoes the instructions writes the empty example first.
    tagged = _TAGGED_RE.findall(text)
    if tagged:
        text = (tagged[-1] or "").strip()
    else:
        # An unterminated <commit> (a truncated answer) still tells us where the
        # message starts; keep everything after it rather than the reasoning above.
        # Split on the same case-insensitive pattern the stray-tag strip uses, so
        # the two can't disagree about what counts as a tag.
        head_and_tail = _STRAY_TAG_RE.split(text, maxsplit=1)
        if len(head_and_tail) > 1:
            text = head_and_tail[1].strip()
        text = _STRAY_TAG_RE.sub("", text).strip()
    if not text:
        return ""
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    lines = text.split("\n")
    # Drop a lead-in, but only while it is the FIRST line (the same words further
    # down are body prose the model chose to write) and only while something is
    # left after it — stripping the only line there is would turn a usable answer
    # into "returned no message".
    while len(lines) > 1 and _PREAMBLE_RE.match(lines[0]):
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    kept: list[str] = []
    for line in lines:
        if _TRAILER_RE.match(line):
            continue
        if line.strip() in ("```", "~~~"):
            continue
        kept.append(line.rstrip())
    # Collapse runs of blank lines: git keeps them and they read as a mistake.
    out: list[str] = []
    for line in kept:
        if not line.strip() and (not out or not out[-1].strip()):
            continue
        out.append(line)
    return "\n".join(out).strip()[:MESSAGE_MAX]


def _run(argv: Sequence[str], cwd: str, timeout: float) -> str:
    """Run a one-shot CLI and return its stdout, or raise with a reason.

    ``stdin`` is closed so a CLI that decides to ask something exits instead of
    hanging until the timeout, and stderr is captured separately so a progress
    spinner never lands in the commit message.
    """
    env = dict(os.environ)
    # These CLIs page their own output when they think they're on a terminal.
    env.setdefault("TERM", "dumb")
    env.setdefault("NO_COLOR", "1")
    try:
        cp = subprocess.run(
            list(argv),
            cwd=cwd or None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise CommitMessageError(
            "%s is not installed" % (argv[0] if argv else "the CLI")
        )
    except subprocess.TimeoutExpired:
        raise CommitMessageError(
            "%s did not answer within %ds" % (argv[0], int(timeout))
        )
    except OSError as err:
        raise CommitMessageError("could not run %s: %s" % (argv[0], err))
    out = cp.stdout.decode("utf-8", "replace")
    if cp.returncode != 0:
        detail = (
            cp.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        )[0]
        raise CommitMessageError(
            "%s exited %d%s"
            % (argv[0], cp.returncode, (": " + detail) if detail else "")
        )
    return out


def pick_argv(prompt: str, program: str, fallback_program: str = "") -> list[str]:
    """The argv to ask ``prompt`` with — the session's CLI, else the default one.

    A session need not run a coding agent at all (``bash`` is a legitimate
    program, and a plain session's is whatever the user typed), and such a session
    still deserves a written commit message. So when its own program can't answer,
    the flock's default CLI does. Raises when neither can.
    """
    from backend import providers

    tried: list[str] = []
    for name in (program, fallback_program):
        provider = providers.resolve(name)
        argv = provider.oneshot_argv(prompt)
        if argv is not None:
            return argv
        label = provider.name or (name or "this CLI")
        if label not in tried:
            tried.append(label)
    raise CommitMessageError(
        "no installed CLI (%s) has a headless mode MindFlock can ask for a commit "
        "message" % ", ".join(tried)
    )


def suggest(
    worktree: str,
    program: str = "",
    timeout: float = TIMEOUT_INTERACTIVE,
    hint: str = "",
    branch: str = "",
    fallback_program: str = "",
) -> str:
    """A commit message for ``worktree``'s uncommitted work.

    Raises :class:`CommitMessageError` for every failure — no CLI to ask, nothing
    to describe, a timeout, an unusable answer — so a caller either gets a real
    message or knows to fall back.
    """
    if not worktree or not os.path.isdir(worktree):
        raise CommitMessageError("workspace not ready")
    stat, patch = collect_diff(worktree)
    if not stat and not patch:
        raise CommitMessageError("nothing to describe — the tree is clean")
    prompt = build_prompt(stat, patch, branch=branch, hint=hint)
    argv = pick_argv(prompt, program, fallback_program)
    message = clean_message(_run(argv, worktree, timeout))
    if not message:
        raise CommitMessageError("%s returned no message" % argv[0])
    return message


def suggest_or_none(
    worktree: str,
    program: str = "",
    timeout: float = TIMEOUT_AUTOPILOT,
    hint: str = "",
    branch: str = "",
    fallback_program: str = "",
) -> Optional[str]:
    """:func:`suggest`, but a failure is ``None`` instead of an exception.

    For the unattended callers (the autopilot's commit step), where the fallback
    is a worse-but-real message and there is nobody to show a reason to.
    """
    try:
        return suggest(
            worktree,
            program=program,
            timeout=timeout,
            hint=hint,
            branch=branch,
            fallback_program=fallback_program,
        )
    except CommitMessageError:
        return None
    except Exception:  # noqa: BLE001 — a subject is never worth failing a commit for
        return None
