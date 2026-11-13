"""The stop cookbook — when each kind of stop fits a route, and the common
routes ready to copy. ONE source of truth for both ways a sentence becomes a
route: the MCP's ``describe_flight`` (an agent building a route for its
person, :mod:`backend.mcp.flights`) and the Sky's "describe it in a sentence"
box (:mod:`backend.web.core.flight_describe`'s one-shot prompt).

Pure data and pure helpers, stdlib only and **no** ``flights`` import: the MCP
stdio server imports this module, and ``flights`` drags FastAPI in (half a
second and ~400 modules per session). The kinds' fields and defaults stay in
``flights.STEP_KINDS``; this file says *when* to use each one. A unit test
fails when a kind lands without an entry here, and every recipe is run through
``flights.normalize_steps``.

* :data:`STOPS` — per kind: ``use`` (when it fits), ``avoid`` (when it does
  not, and what to use instead), ``fields`` (the typical config), ``waits``
  (what it waits on), ``fixable`` (``on_fail: "agent"`` makes sense: the
  agent is handed the failure and the stop runs again, at most 2 rounds) and
  ``rare`` (fits few routes: never add it unless the person asked for it).
* :data:`RECIPES` — common situations as whole routes.
* :data:`RULES` — the order of a route and when to ask instead of guessing.
* :func:`lint` — the mistakes a route can make (a Join with no Split, Merge
  before Open PR …), each with what to do instead.
* :func:`sentence` — a route read back as one line, for the person to OK.

Words: docs/vocabulary.md (route, stop, saved route, Wait for me, Halt, Needs
you, Verify, Goes as far as). The wire names stay (``kind``, ``steps``,
``on_fail``).
"""

from __future__ import annotations

import difflib
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

__all__ = [
    "STOPS",
    "RECIPES",
    "RULES",
    "ALIASES",
    "stop_line",
    "options_line",
    "recipe_line",
    "lint",
    "kind_fields",
    "unknown_option_text",
    "LEGACY_OPTIONS",
    "errors",
    "sentence",
    "notation",
    "issue_line",
    "step_phrase",
    "closest_kind",
    "field_hint",
    "prompt_text",
]

#: The ship ladder, lowest rung first (``flights.SHIP_LANES``).
LANES = ("commit", "push", "pr", "merge")
LANE_WORD = {
    "commit": "Commit",
    "push": "Push",
    "pr": "Open PR",
    "merge": "Merge when green",
}
#: The fix rounds an ``on_fail: "agent"`` stop gets (``flights.MAX_FIX_ROUNDS``).
FIX_ROUNDS = 2


def _s(use, avoid, fields, waits, fixable=False, rare=False, means="") -> dict:
    return {
        "use": use,
        "avoid": avoid,
        "fields": fields,
        "waits": waits,
        "fixable": fixable,
        "rare": rare,
        "means": means,
    }


#: kind -> when it fits. Keep every line short: the MCP hands all of them to an
#: agent in one tool answer.
STOPS: Dict[str, dict] = {
    "agent": _s(
        "the coding work itself: the task, a fix, a follow-up prompt",
        "running tests (test) or a shell command (command); on the session you "
        "are in, the work so far already counts as the first agent stop",
        "prompt ({ticket} {pr} {branch} filled in); fresh=true = a new session",
        "the agent's turn to end, and its workers",
    ),
    "split": _s(
        "the task falls into independent pieces for parallel workers",
        "one coherent change; always put a join right after it",
        "prompts (one worker per line), fence_only (paths each may change)",
        "nothing: the workers start",
    ),
    "join": _s(
        "right after a split: wait until its workers finish (of=spawned)",
        "without a split before it (an agent stop already waits for its own "
        "workers)",
        "of=spawned|all, on_failed=halt|continue",
        "the workers",
    ),
    "ship": _s(
        "commit / push / open the PR / merge when green: lane=commit|push|pr|"
        "merge. A rung includes the ones below (lane=pr commits and pushes)",
        "merge unless the person asked for it (needs confirm_merge=true); "
        "rungs out of order",
        "lane; ask_first=true asks the person before it leaves the machine",
        "merge: CI to go green, then merges",
        fixable=True,
    ),
    "verify": _s(
        "after the agent's work, before the PR or merge: the session's own "
        "agent checks its work in real use (it writes its own steps)",
        "unit tests (test); with no agent work before it",
        "where=branch or an environment, ready=now|on_branch|after_prev, "
        "ready_minutes, who=both|agent|you, ask=here|end, notes, "
        "post_to_pr=true to comment the results on the PR",
        "the agent's check; then you, for who=both on person steps, who=you always",
        fixable=True,
        means="where=branch is this branch's own code (no deploy), else one "
        "of the repo's environments (a deployed place with its own branch, "
        "e.g. live, sandbox); ready on_branch = once the work reaches the "
        "environment's branch, after_prev = ready_minutes after the stop "
        "before; who: both = agent, then you (it parks on you only for the "
        "steps a person must do); agent = agent only (it attempts every step "
        "itself and must give evidence; parks only when one ends blocked or "
        "failed); you = agent, then your sign-off (always waits for your "
        "confirm); ask: here = the route waits at this stop when the check "
        "needs you, end = it carries on and asks you once before the route "
        "finishes (with the next Wait-for-me stop it reaches, else a stop added at the "
        "end; a check you say doesn't work sends the route back here)",
    ),
    "you": _s(
        "Wait for me: only a person can look, decide or approve (approve the "
        "merge, try it on a phone)",
        "a plain heads-up (notify); something a stop can check",
        "text (what to do), items (things to tick off)",
        "the person pressing Done; it never fails",
    ),
    "wait": _s(
        "pause for a time, or until a time of day (let a deploy settle)",
        "waiting for CI (checks), reviews (reviews) or a person (you)",
        "seconds, or until=HH:MM",
        "the clock",
    ),
    "reviews": _s(
        "after Open PR: send review comments (CodeRabbit, people; those "
        "already open first) to the agent and push, until the PR goes quiet",
        "before a PR exists",
        "quiet_minutes=10, max_rounds=5, include_bots=true; "
        "require_reviewer=coderabbit (+ reviewer_wait_minutes=10) waits for "
        "that reviewer to review the PR's latest commit (again after each "
        "push) and fails if it never does; "
        "each_round='…' adds the route's own instructions to every round "
        "(re-run a review before the push, merge the base on conflict); "
        "reply_declined=true has the agent answer declined comments on the PR",
        "the PR to go quiet (and the required reviewer)",
    ),
    "checks": _s(
        "after a push / Open PR: wait for CI to pass; on_fail=agent lets the "
        "agent fix red CI",
        "before a PR or push exists (Merge when green waits for green itself)",
        "timeout_minutes=60",
        "CI",
        fixable=True,
    ),
    "command": _s(
        "one shell command in the worktree (build, lint, a script); non-zero "
        "exit fails it",
        "the project's tests (test reads the results); deploys that print a "
        "URL (preview)",
        "command (required), timeout_minutes",
        "the command",
        fixable=True,
    ),
    "ticket": _s(
        "move the route's ticket to a state and/or comment on it",
        "a route with no ticket (it is skipped)",
        "state (exact name, e.g. Done), comment",
        "nothing",
    ),
    "notify": _s(
        "tell the person something (phone + bell) without stopping",
        "when the route must wait for them (you)",
        "text",
        "nothing",
    ),
    "test": _s(
        "run the project's tests; on_fail=agent = the agent fixes failures "
        "(%d rounds)" % FIX_ROUNDS,
        "a one-off script (command); checking the work in real use (verify)",
        "command (empty = found), expect=fail for write-the-failing-test-first",
        "the tests",
        fixable=True,
    ),
    "review": _s(
        "a second agent reviews the diff before the PR — mode=bugs, security "
        "or standards (the team's rules in focus); findings go to the agent "
        "to fix or rebut with on_fail=agent, and each later round only "
        "checks the fixes",
        "answering PR review comments (reviews)",
        "mode=bugs|security|standards, focus (the rules), block=true, "
        "rounds=3 (fix rounds, 1-10), unresolved=ask|continue (findings "
        "left after the last round: ask the person, or carry on)",
        "the reviewer",
        fixable=True,
    ),
    "loop": _s(
        "repeat earlier stops: back_to=<stop id> or 'agent', times=N, "
        "until=<condition>, every_seconds for a pace",
        "a test fix-up (test with on_fail=agent does that); times=0 with no "
        "until runs forever",
        "back_to, times=3, until (e.g. judge is yes), prompt for back_to=agent",
        "the stops it repeats",
    ),
    "judge": _s(
        "ask one yes/no question about the work; a later stop's when='judge is "
        "yes' reads it",
        "a check a test or command can do",
        "question, options='yes, no'",
        "a model's answer",
    ),
    "sync": _s(
        "rebase the branch onto main (or merge main in) before shipping; a "
        "conflict goes to the agent",
        "routes that open a fresh branch and ship at once",
        "with (empty = default branch), how=rebase|merge",
        "git",
        fixable=True,
    ),
    "land": _s(
        "Finish: after Merge when green, delete the branch and close the "
        "session; always the last stop",
        "anything but the very end; routes that do not merge",
        "delete_branch, close=delete|close",
        "the merge",
    ),
    "handoff": _s(
        "start another saved route (on a new session by default)",
        "work that fits on this route",
        "target (a saved route id), wait",
        "the other route if wait=true",
        rare=True,
    ),
    "someone": _s(
        "a person OTHER than the user (a peer, a Slack webhook) must check or "
        "do something",
        "the user themselves (you)",
        "who or webhook, text",
        "them pressing Done",
        rare=True,
    ),
    "preview": _s(
        "deploy a preview with the person's own command and keep its URL",
        "projects with no preview deploy command — never invent one",
        "command (required: prints the URL)",
        "the deploy",
        fixable=True,
        rare=True,
    ),
    "screenshot": _s(
        "pictures of pages at widths, when the person asks for screenshots "
        "(after a preview)",
        "almost always: not a test, not a review; needs a URL or a preview",
        "urls ({preview}), widths",
        "headless Chrome",
        rare=True,
    ),
    "listen": _s(
        "wait for an outside system to call this stop's URL (a webhook)",
        "almost always: CI is checks, a person is you, time is wait",
        "match, timeout_minutes",
        "the webhook",
        rare=True,
    ),
    "call": _s(
        "send one HTTP request (deploy hook, Slack, an API) the person named",
        "almost always: never invent a URL; telling the person is notify",
        "method, url (required), body",
        "the request",
        rare=True,
    ),
    "budget": _s(
        "the person set a cost/time limit (dollars, minutes, tokens)",
        "almost always: only when they named a limit",
        "max_usd / max_minutes / max_tokens, scope=flight",
        "nothing (it watches)",
        rare=True,
    ),
    "checkpoint": _s(
        "remember the worktree so the person can rewind to it later",
        "almost always: commits already keep the work; only when asked",
        "note",
        "nothing",
        rare=True,
    ),
}

#: Words people (and agents) use for a stop that is not its kind's name.
ALIASES: Dict[str, str] = {
    "pr": "ship (lane pr)",
    "open_pr": "ship (lane pr)",
    "pull_request": "ship (lane pr)",
    "merge": "ship (lane merge)",
    "commit": "ship (lane commit)",
    "push": "ship (lane push)",
    "deploy": "preview or command",
    "approve": "you",
    "approval": "you",
    "wait_for_me": "you",
    "human": "you",
    "person": "you",
    "perch": "you",
    "ask": "you",
    "tests": "test",
    "pytest": "test",
    "ci": "checks",
    "coderabbit": "reviews",
    "finish": "land",
    "close": "land",
    "workers": "split",
    "fan_out": "split",
    "fanout": "split",
    "repeat": "loop",
    "rebase": "sync",
    "sleep": "wait",
    "delay": "wait",
    "shell": "command",
    "run": "command",
    "slack": "notify (or call with the person's webhook)",
    "webhook": "listen or call",
}

#: A route's order and the asks, terse: handed to the agent and to the
#: one-shot prompt.
RULES: List[str] = [
    "Order: agent → test/review/verify → ship pr → reviews/checks → you → "
    "ship merge → ticket/land. Leave out what the person did not ask for.",
    "One ship stop per rung, rising: commit < push < pr < merge. Merge when "
    "green already pushes, opens the PR and waits for green CI.",
    "Use on_fail=agent on test, checks, review, command, verify: the agent fixes "
    "the failure (%d rounds) and the stop runs again. Default halt = the route "
    "waits for the person to Retry/Skip." % FIX_ROUNDS,
    "'Let me look first' / 'after my OK' = a you stop (Wait for me) before the "
    "stop it guards. Merge only when asked (start_flight confirm_merge=true).",
    "A Split always gets a Join right after; a Loop sits AFTER the stops it "
    "repeats; Finish (land) is always last.",
    "Ask the person instead of guessing when: they want a merge but did not "
    "say who OKs it; a schedule has no time; a webhook/URL/deploy command is "
    "needed but not given; the request is not a route at all.",
]

# Recipes: a step is (kind, title, config, on_fail). Titles stay short.
_R = lambda kind, title="", on_fail="halt", **config: {  # noqa: E731
    k: v
    for k, v in (
        ("kind", kind),
        ("title", title),
        ("config", config),
        ("on_fail", on_fail),
    )
    if v not in ("", {}) and not (k == "on_fail" and v == "halt")
}

#: Common situations, each a whole route. ``when`` is the situation in the
#: person's words; ``trigger`` (optional) is what starts it as a saved route.
RECIPES: List[dict] = [
    {
        "id": "ticket_pr_tests",
        "when": "work a ticket, test it, open a PR",
        "steps": [
            _R("agent", "Work the ticket", prompt="{ticket}"),
            _R("test", "Tests pass", on_fail="agent"),
            _R("ship", "Open PR", lane="pr"),
        ],
    },
    {
        "id": "ticket_merge_after_ok",
        "when": "a ticket all the way to merged, but only after my OK",
        "steps": [
            _R("agent", "Work the ticket", prompt="{ticket}"),
            _R("test", "Tests pass", on_fail="agent"),
            _R("ship", "Open PR", lane="pr"),
            _R("checks", "CI checks", on_fail="agent"),
            _R("you", "Approve the merge", text="Look over the PR, then press Done"),
            _R("ship", "Merge when green", lane="merge"),
            _R("ticket", "Ticket: Done", state="Done"),
        ],
    },
    {
        "id": "pr_review_coderabbit",
        "when": "answer the PR's review comments (CodeRabbit too) until CI is green",
        "trigger": {"kind": "pr"},
        "steps": [
            _R(
                "agent",
                "Address the reviews",
                prompt="Address the review comments on {pr}",
            ),
            _R("ship", "Push", lane="push"),
            _R("reviews", "Reviews settle", include_bots=True),
            _R("checks", "CI checks", on_fail="agent"),
        ],
    },
    {
        "id": "nightly_chores",
        "when": "chores every night on a schedule (a saved route)",
        "trigger": {"kind": "schedule", "schedule": {"at": "02:00", "days": "daily"}},
        "steps": [
            _R(
                "agent",
                "Nightly chores",
                prompt="Update dependencies and fix what breaks",
            ),
            _R("test", "Tests pass", on_fail="agent"),
            _R("ship", "Open PR", lane="pr"),
            _R("notify", "Tell me", text="Nightly chores PR is up"),
        ],
    },
    {
        "id": "split_and_join",
        "when": "split the task into parallel workers, then bring it together",
        "steps": [
            _R("split", "Split", prompts="Do part A\nDo part B\nDo part C"),
            _R("join", "Join", of="spawned"),
            _R(
                "agent",
                "Bring it together",
                prompt="Merge the workers' branches and tidy up",
            ),
            _R("test", "Tests pass", on_fail="agent"),
            _R("ship", "Open PR", lane="pr"),
        ],
    },
    {
        "id": "loop_until_green",
        "when": "keep the agent at it until the tests pass (more than 2 tries)",
        "steps": [
            _R("agent", "Fix it", prompt="Make the failing tests pass"),
            _R("test", "Tests", on_fail="skip"),
            _R("judge", "All green?", question="Do all the project's tests pass now?"),
            _R(
                "loop",
                "Again until green",
                back_to="s1",
                times=5,
                until="judge is yes",
            ),
        ],
    },
    {
        "id": "verify_before_pr",
        "when": "the agent checks its own work in real use before the PR",
        "steps": [
            _R("agent", "Work the ticket", prompt="{ticket}"),
            _R(
                "verify",
                "Verify",
                on_fail="agent",
                who="both",
                notes="Try the change the way a user would",
            ),
            _R("ship", "Open PR", lane="pr"),
        ],
    },
    {
        "id": "verify_on_env_after_merge",
        "when": "merge after my OK, then check it once it is live",
        "steps": [
            _R("agent", "Do the work"),
            _R("test", "Tests pass", on_fail="agent"),
            _R("ship", "Open PR", lane="pr"),
            _R("you", "Approve the merge", text="Look over the PR, then press Done"),
            _R("ship", "Merge when green", lane="merge"),
            _R(
                "verify",
                "Verify on live",
                who="you",
                where="live",
                ready="on_branch",
                ready_minutes=5,
            ),
        ],
    },
    {
        "id": "bot_pr_merge_green",
        "when": "a bot PR (Dependabot/Renovate) merges itself when green",
        "trigger": {"kind": "pr"},
        "steps": [
            _R("checks", "CI checks", on_fail="agent"),
            _R("ship", "Merge when green", lane="merge"),
        ],
    },
    {
        "id": "release",
        "when": "cut a release: notes + version bump, merged after my OK",
        "steps": [
            _R("sync", "Up to date with main"),
            _R(
                "agent",
                "Release notes + bump",
                prompt="Write the release notes and bump the version",
            ),
            _R("test", "Tests pass", on_fail="agent"),
            _R("ship", "Open PR", lane="pr"),
            _R("checks", "CI checks", on_fail="agent"),
            _R("you", "OK the release", text="Read the notes, then press Done"),
            _R("ship", "Merge when green", lane="merge"),
            _R("notify", "Tag it", text="Merged: tag the release"),
        ],
    },
    {
        "id": "test_first",
        "when": "write a failing test first, then fix it, review, PR",
        "steps": [
            _R(
                "agent",
                "Write the failing test",
                prompt="Write a test that shows the bug",
            ),
            _R("test", "It fails", expect="fail"),
            _R("agent", "Fix the bug", prompt="Make that test pass"),
            _R("test", "Tests pass", on_fail="agent"),
            _R("review", "Second opinion", on_fail="agent"),
            _R("ship", "Open PR", lane="pr"),
        ],
    },
    {
        "id": "team_rules_review_bot_loop",
        "when": "self-review against the team's rules, then a review-bot loop "
        "(CodeRabbit) on the PR",
        "steps": [
            _R("agent", "Work the ticket", prompt="{ticket}"),
            _R("test", "Tests pass", on_fail="agent"),
            _R(
                "review",
                "Standards review",
                on_fail="agent",
                mode="standards",
                focus=(
                    "Every change has unit tests and evidence it was tested "
                    "locally. New code paths log meaningfully and emit metrics."
                ),
            ),
            _R("review", "Security review", on_fail="agent", mode="security"),
            _R("ship", "Open PR", lane="pr"),
            _R(
                "reviews",
                "Review bot loop",
                on_fail="you",
                require_reviewer="coderabbit",
                reviewer_wait_minutes=10,
                quiet_minutes=5,
                max_rounds=5,
            ),
            _R("checks", "CI checks", on_fail="agent"),
        ],
    },
]

#: How a review stop reads, by ``mode`` (``flight_kinds.review.MODES``).
REVIEW_MODE_WORD = {
    "bugs": "Second opinion",
    "security": "Security review",
    "standards": "Standards review (the team's rules)",
}


# --------------------------------------------------------------------------- #
# Compact lines (the MCP's answer, the one-shot prompt)
# --------------------------------------------------------------------------- #
def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def options_line(k: dict) -> str:
    """One kind's options as ``GET /api/flights/kinds`` defines them, with
    each default and, for a choice, its allowed values (and what each means):
    ``lane=push (commit|push|pr|merge); ask_first=false; message`` — plus
    ``on_fail=…`` when the kind's default is not halt. ``"none"`` when the
    kind has no options."""
    parts: List[str] = []
    for f in k.get("fields") or []:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        part = str(f["name"])
        if f.get("default") not in (None, ""):
            part += "=" + _value(f["default"])
        opts = [str(o) for o in f.get("options") or []]
        if opts:
            words = (
                f.get("option_labels")
                if isinstance(f.get("option_labels"), dict)
                else {}
            )
            part += " (%s)" % " | ".join(
                "%s = %s" % (o, words[o]) if words.get(o) else o for o in opts
            )
        elif f.get("dynamic") == "stops_before":
            part += " (an earlier stop id, or agent)"
        parts.append(part)
    if (k.get("default_on_fail") or "halt") != "halt":
        parts.append("on_fail=%s" % k["default_on_fail"])
    return "; ".join(parts) or "none"


def stop_line(kind: str, label: str = "", options: str = "") -> str:
    """``kind · use … · not … · fields … [· rare]`` — one terse line.
    ``options`` (:func:`options_line`, from the server's kinds) replaces the
    cookbook's typical-config words and comes first, so a line cut short
    keeps it."""
    s = STOPS.get(kind)
    if s is None:
        return ""
    head = kind if not label or label.lower() == kind else "%s (%s)" % (kind, label)
    if options:
        head += " · options: " + options
    line = "%s · use: %s · not: %s · %s · %s · waits: %s" % (
        head,
        s["use"],
        s["avoid"],
        "" if options else s["fields"],
        ("means: " + s["means"]) if s.get("means") else "",
        s["waits"],
    )
    while " ·  · " in line:
        line = line.replace(" ·  · ", " · ")
    if s["fixable"]:
        line += " · on_fail=agent ok"
    if s["rare"]:
        line += " · RARE"
    return line


def _step_json(step: dict) -> str:
    return json.dumps(step, separators=(",", ":"), ensure_ascii=False)


def notation(steps: Sequence[dict], width: int = 40) -> str:
    """Steps in a terse exact form an agent can copy: ``agent(prompt="…") →
    test!agent → ship(lane=pr)`` — ``!x`` is ``on_fail: x``, ``?cond`` a
    ``when``."""
    out = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        cfg = ", ".join(
            "%s=%s"
            % (
                k,
                (
                    json.dumps(
                        v if len(v) <= width else v[: width - 1] + "…",
                        ensure_ascii=False,
                    )
                    if isinstance(v, str)
                    else json.dumps(v)
                ),
            )
            for k, v in _cfg(s).items()
        )
        part = str(s.get("kind") or "?") + ("(%s)" % cfg if cfg else "")
        if s.get("on_fail") and s.get("on_fail") != "halt":
            part += "!" + str(s["on_fail"])
        if s.get("when"):
            part += "?" + _cut(s["when"], width)
        out.append(part)
    return " → ".join(out)


def recipe_line(recipe: dict) -> str:
    """``id — when: <notation>`` (+ its trigger)."""
    line = "%s — %s: %s" % (recipe["id"], recipe["when"], notation(recipe["steps"]))
    trig = recipe.get("trigger")
    if trig:
        line += " · trigger %s" % _step_json(trig)
    return line


# --------------------------------------------------------------------------- #
# The read-back sentence (the dry run)
# --------------------------------------------------------------------------- #
def _cfg(step: dict) -> dict:
    c = step.get("config")
    return c if isinstance(c, dict) else {}


def _cut(text: Any, n: int) -> str:
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _dur(seconds: int) -> str:
    if seconds % 3600 == 0 and seconds >= 3600:
        return "%d h" % (seconds // 3600)
    if seconds % 60 == 0 and seconds >= 60:
        return "%d min" % (seconds // 60)
    return "%d s" % seconds


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return default


#: A Verify stop's ``who`` in a sentence (the editor's labels say "me").
VERIFY_WHO_WORDS = {
    "both": "agent, then you",
    "agent": "agent only",
    "you": "agent, then your sign-off",
}


def verify_sentence(config: dict, env_branch: str = "") -> str:
    """A Verify stop in words, as its face says it: ``Verify on sandbox ·
    agent, then you · 15 min after the stop before`` (``env_branch``: the
    environment's branch, when known, for "once on <branch>"). Reads an old
    ``wait_live`` config too."""
    c = config if isinstance(config, dict) else {}
    legacy = bool(c.get("wait_live"))
    where = str(c.get("where") or ("live" if legacy else "branch"))
    ready = str(c.get("ready") or ("on_branch" if legacy else "now"))
    minutes = max(0, min(1440, _int(c.get("ready_minutes"), 0)))
    place = "this branch" if where == "branch" else where
    who = VERIFY_WHO_WORDS.get(str(c.get("who") or "both"), "agent, then you")
    if ready == "on_branch" and where != "branch":
        when = "once on %s" % (env_branch or "the %s branch" % where)
        if minutes:
            when += " + %d min" % minutes
    elif ready == "after_prev":
        when = (
            "%d min after the stop before" % minutes
            if minutes
            else "after the stop before, once %s has deployed" % place
        )
    else:
        when = "right away"
    return "Verify on %s · %s · %s" % (place, who, when)


def step_phrase(step: dict, labels: Optional[Dict[str, str]] = None) -> str:
    """One stop in words: ``Test (agent fixes failures ×2)``."""
    kind = str(step.get("kind") or "?")
    c = _cfg(step)
    title = _cut(step.get("title"), 50)
    if kind == "agent":
        what = _cut(c.get("prompt"), 50)
        if not what or not re.sub(r"\{\w+\}", "", what).strip():
            what = (
                title  # a prompt of placeholders only ("{ticket}") reads as its title
            )
        p = "Agent" + (
            ": " + what if what and what.lower() not in ("agent", "agent works") else ""
        )
        if c.get("fresh"):
            p += " (new session)"
    elif kind == "ship":
        lane = str(c.get("lane") or "push")
        p = LANE_WORD.get(lane, "Ship " + lane)
        if c.get("ask_first"):
            p += " (asks you first)"
    elif kind == "you":
        what = _cut(c.get("text"), 50) or title
        p = "waits for you" + (": " + what if what else "")
    elif kind == "wait":
        if c.get("until"):
            p = "Wait until %s" % c["until"]
        else:
            secs = _int(c.get("seconds"), 0) or _int(c.get("minutes"), 0) * 60 or 600
            p = "Wait %s" % _dur(secs)
    elif kind == "test":
        p = "Test" + (" (expects a failure)" if c.get("expect") == "fail" else "")
    elif kind == "checks":
        p = "CI checks"
    elif kind == "reviews":
        p = "Reviews settle"
        if c.get("require_reviewer"):
            p += " (waits for %s)" % _cut(c["require_reviewer"], 30)
    elif kind == "review":
        p = REVIEW_MODE_WORD.get(str(c.get("mode") or "bugs"), "Second opinion")
    elif kind == "verify":
        p = verify_sentence(c)
        if c.get("ask") == "end":
            p += " (asks you at the end)"
        if c.get("post_to_pr"):
            p += " (posts the results to the PR)"
    elif kind == "split":
        lines = [ln for ln in str(c.get("prompts") or "").splitlines() if ln.strip()]
        n = len(lines) if len(lines) > 1 else max(1, _int(c.get("count"), 1))
        p = "Split into %d worker%s" % (n, "" if n == 1 else "s")
    elif kind == "join":
        p = "Join (waits for the workers)"
    elif kind == "loop":
        to = str(c.get("back_to") or "")
        p = "Loop back to " + ("the agent" if to == "agent" else (to or "the start"))
        times = _int(c.get("times"), 3)
        p += " ×%d" % times if times else " (no limit)"
        if c.get("until"):
            p += " until %s" % _cut(c["until"], 40)
    elif kind == "command":
        p = "Run `%s`" % _cut(c.get("command"), 40)
    elif kind == "ticket":
        bits = []
        if c.get("state"):
            bits.append("move to %s" % c["state"])
        if c.get("comment"):
            bits.append("comment")
        p = "Ticket: " + (" + ".join(bits) or "comment")
    elif kind == "notify":
        p = "Notify you" + (": " + _cut(c.get("text"), 40) if c.get("text") else "")
    elif kind == "land":
        p = "Finish (delete the branch, close the session)"
    elif kind == "judge":
        p = "Judge: %s" % (_cut(c.get("question"), 50) or title)
    else:
        label = (labels or {}).get(kind) or kind.capitalize()
        p = label + (": " + title if title and title.lower() != label.lower() else "")
    of = str(step.get("on_fail") or "halt")
    if kind != "you":
        if of == "agent":
            p += " (agent fixes failures ×%d)" % FIX_ROUNDS
        elif of == "skip":
            p += " (skipped if it fails)"
        elif of == "you":
            p += " (asks you if it fails)"
    if step.get("when"):
        p += " — only when %s" % _cut(step["when"], 50)
    if step.get("halt"):
        p = "[halts here] " + p
    return p


def sentence(steps: Sequence[dict], labels: Optional[Dict[str, str]] = None) -> str:
    """The route as one line to read back to the person: ``Agent: fix the
    login bug → Test (agent fixes failures ×2) → Open PR → waits for you →
    Merge when green``."""
    return (
        " → ".join(step_phrase(s, labels) for s in steps if isinstance(s, dict))
        or "(no stops)"
    )


# --------------------------------------------------------------------------- #
# Lint: the mistakes, each with what to do instead
# --------------------------------------------------------------------------- #
def closest_kind(kind: str, known: Iterable[str]) -> str:
    """What an unknown kind most likely meant (``""``: no idea)."""
    k = re.sub(r"[\s-]+", "_", str(kind or "").strip().lower())
    if k in ALIASES:
        return ALIASES[k]
    hit = difflib.get_close_matches(k, list(known), n=1, cutoff=0.6)
    return hit[0] if hit else ""


def _issue(
    level: str, i: int, step: dict, text: str, field: str = "", sid: str = ""
) -> dict:
    sid = sid or str(step.get("id") or "") or "steps[%d]" % i
    out = {"level": level, "at": i, "stop": sid, "text": text}
    if field:
        out["field"] = field
    return out


#: Options a kind still reads under an older name (``flights._step_config``
#: and the Loop plug-in): never "unknown", though no form shows them.
LEGACY_OPTIONS: Dict[str, tuple] = {
    "wait": ("minutes",),
    "verify": ("wait_live",),
    "loop": ("every_minutes",),
}


def kind_fields(dto: dict) -> Dict[str, List[str]]:
    """``{kind: [option names]}`` from ``GET /api/flights/kinds`` (the
    kinds' own definitions — never a hand-kept copy)."""
    out: Dict[str, List[str]] = {}
    for k in (dto or {}).get("kinds") or []:
        if isinstance(k, dict) and k.get("kind"):
            out[str(k["kind"])] = [
                str(f["name"])
                for f in k.get("fields") or []
                if isinstance(f, dict) and f.get("name")
            ]
    return out


def _a(label: str) -> str:
    return ("an " if label[:1].lower() in "aeiou" else "a ") + label


def unknown_option_text(option: str, label: str, names: Sequence[str]) -> str:
    """The server's words for an option a kind does not have: ``unknown
    option 'timeout' on a CI checks stop — it has: timeout_minutes``."""
    return "unknown option %r on %s stop — it has: %s" % (
        option,
        _a(label),
        ", ".join(names) or "no options",
    )


def _server_ids(rows: Sequence[dict], renumber: Optional[int]) -> List[str]:
    """The ids the server gives ``rows``: as written (``renumber`` None — a
    saved route keeps valid ids), or, from index ``renumber`` on, the next
    free ``sN`` after the ids before it (a start's or an add's new stops)."""
    ids = [str(s.get("id") or "") for s in rows]
    if renumber is None:
        # A stop with no id (or a taken one) gets s<its place>, as the
        # normaliser numbers it.
        taken: set = set()
        for k, sid in enumerate(ids):
            if not re.match(r"^[A-Za-z0-9_-]{1,32}$", sid) or sid in taken:
                n = k + 1
                while "s%d" % n in taken:
                    n += 1
                sid = "s%d" % n
            taken.add(sid)
            ids[k] = sid
        return ids
    n = 0
    for sid in ids[:renumber]:
        m = re.match(r"^s(\d+)$", sid)
        n = max(n, int(m.group(1)) if m else 0)
    out = ids[:renumber]
    for _ in rows[renumber:]:
        n += 1
        out.append("s%d" % n)
    return out


def lint(
    steps: Sequence[dict],
    known: Optional[Iterable[str]] = None,
    on_session: bool = True,
    has_pr: bool = False,
    fields: Optional[Dict[str, Sequence[str]]] = None,
    labels: Optional[Dict[str, str]] = None,
    renumber: Optional[int] = None,
) -> List[dict]:
    """The mistakes in ``steps`` (raw, as an agent writes them, or
    normalised): ``[{level: error|warn, at, stop, text}]``. ``error`` = the
    route can't work as written (the tools refuse it); ``warn`` = odd, read it
    back to the person. Every ``text`` says what to do instead. ``known``: the
    kinds the server serves (default: the cookbook's). ``on_session``: the
    route runs on an existing session (its work so far counts as the agent).
    ``has_pr``: the route's subject is a pull request already (a PR route).
    ``fields`` (``{kind: [option]}``, :func:`kind_fields`): an option a kind
    does not have is an error, in the server's words (``labels`` names the
    kind). ``renumber``: the stops from this index on get the server's next
    ``sN`` (a start's or an add's new stops; their own ids are dropped), so a
    Loop's ``back_to`` naming one by the author's id is an error."""
    known_set = set(known if known is not None else STOPS)
    out: List[dict] = []
    rows = [s if isinstance(s, dict) else {} for s in steps]
    kinds = [str(s.get("kind") or "").strip().lower() for s in rows]
    given = [str(s.get("id") or "") for s in rows]
    ids = _server_ids(rows, renumber)
    renamed = {
        g: ids[k]
        for k, g in enumerate(given)
        if renumber is not None and k >= renumber and g and g != ids[k]
    }
    if not rows:
        return [
            {
                "level": "error",
                "at": 0,
                "stop": "",
                "text": "a route needs at least one stop",
            }
        ]
    top_lane = -1
    top_lane_at = -1
    have_pr = bool(has_pr)
    for i, (s, kind) in enumerate(zip(rows, kinds)):
        c = _cfg(s)
        before = kinds[:i]
        if kind not in known_set:
            hint = closest_kind(kind, known_set)
            out.append(
                _issue(
                    "error",
                    i,
                    s,
                    "no stop kind %r%s" % (kind, " — use %s" % hint if hint else ""),
                )
            )
            continue
        names = (fields or {}).get(kind)
        if names is not None and isinstance(s.get("config"), dict):
            allowed = set(names) | set(LEGACY_OPTIONS.get(kind, ()))
            label = (labels or {}).get(kind) or kind
            for opt in s["config"]:
                if opt not in allowed:
                    out.append(
                        _issue(
                            "error",
                            i,
                            s,
                            unknown_option_text(str(opt), label, list(names)),
                            "steps[%d].config.%s" % (i, opt),
                            ids[i],
                        )
                    )
        of = str(s.get("on_fail") or "halt")
        info = STOPS.get(kind) or {}
        if of == "agent" and info and not info.get("fixable") and kind != "you":
            out.append(
                _issue(
                    "warn",
                    i,
                    s,
                    "on_fail=agent does nothing useful on %s (the agent can't fix "
                    "it) — leave on_fail as halt" % kind,
                )
            )
        if kind == "ship":
            lane = str(c.get("lane") or "push").lower()
            if lane not in LANES:
                out.append(
                    _issue(
                        "error",
                        i,
                        s,
                        "lane %r — a ship stop's lane is commit, push, pr or merge"
                        % lane,
                    )
                )
                continue
            rung = LANES.index(lane)
            if rung < top_lane:
                out.append(
                    _issue(
                        "error",
                        i,
                        s,
                        "%s after %s goes backwards — %s already did it: drop this "
                        "stop or move it before s%s"
                        % (
                            LANE_WORD[lane],
                            LANE_WORD[LANES[top_lane]],
                            LANE_WORD[LANES[top_lane]],
                            top_lane_at + 1,
                        ),
                    )
                )
            elif rung == top_lane:
                out.append(
                    _issue("warn", i, s, "%s twice — drop one" % LANE_WORD[lane])
                )
            else:
                top_lane, top_lane_at = rung, i
            if lane in ("pr", "merge"):
                have_pr = True
        elif kind == "join":
            if "split" not in before:
                if str(c.get("of") or "all") == "spawned":
                    out.append(
                        _issue(
                            "error",
                            i,
                            s,
                            "Join of=spawned waits for a Split's workers but no "
                            "Split comes before it — add a split stop before it, "
                            "or drop the Join",
                        )
                    )
                else:
                    out.append(
                        _issue(
                            "warn",
                            i,
                            s,
                            "a Join with no Split before it — an agent stop already "
                            "waits for its own workers; drop it unless the agent "
                            "spawns workers itself",
                        )
                    )
        elif kind == "split":
            prompts = c.get("prompts")
            if isinstance(prompts, list):
                prompts = "\n".join(str(p) for p in prompts)
            if not str(prompts or "").strip():
                out.append(
                    _issue(
                        "error",
                        i,
                        s,
                        "a Split needs config.prompts: one worker's task per line",
                    )
                )
            rest = kinds[i + 1 :]
            if "join" not in rest and "agent" not in rest:
                out.append(
                    _issue(
                        "warn",
                        i,
                        s,
                        "nothing waits for the Split's workers — put a join "
                        "(of=spawned) right after it",
                    )
                )
        elif kind in ("reviews", "checks"):
            if not have_pr and not (
                kind == "checks" and top_lane >= LANES.index("push")
            ):
                out.append(
                    _issue(
                        "warn",
                        i,
                        s,
                        "%s waits on a PR but no Open PR comes before it — put "
                        "ship lane=pr before it (fine if the session already has "
                        "a PR)"
                        % ("Reviews settle" if kind == "reviews" else "CI checks"),
                    )
                )
        elif kind == "verify":
            if "agent" not in before and "split" not in before and not on_session:
                out.append(
                    _issue(
                        "warn",
                        i,
                        s,
                        "Verify with no agent work before it checks an empty branch "
                        "— put the agent stop first",
                    )
                )
        elif kind == "you":
            if not str(c.get("text") or "").strip() and not c.get("items"):
                out.append(
                    _issue(
                        "warn",
                        i,
                        s,
                        "a Wait for me with no text: say what the person should "
                        "do before pressing Done (config.text)",
                    )
                )
        elif kind == "land":
            if i != len(rows) - 1:
                out.append(
                    _issue(
                        "error",
                        i,
                        s,
                        "Finish (land) closes the session, so it is the last "
                        "stop — move it to the end",
                    )
                )
            if top_lane < LANES.index("merge"):
                out.append(
                    _issue(
                        "warn",
                        i,
                        s,
                        "Finish waits for the branch to be merged but nothing "
                        "merges it — add ship lane=merge before it",
                    )
                )
        elif kind == "loop":
            to = str(c.get("back_to") or "")
            if to in renamed:
                out.append(
                    _issue(
                        "error",
                        i,
                        s,
                        "back_to %r is your own id for a stop, but the server "
                        "numbers new stops s1, s2… in order: that stop will be "
                        "%s — use back_to=%r" % (to, renamed[to], renamed[to]),
                        "steps[%d].config.back_to" % i,
                        ids[i],
                    )
                )
            elif to and to != "agent":
                if to in ids[i:]:
                    out.append(
                        _issue(
                            "error",
                            i,
                            s,
                            "a Loop goes back to an EARLIER stop; %s is not before "
                            "it — move the Loop after %s" % (to, to),
                            "steps[%d].config.back_to" % i,
                        )
                    )
                elif to not in ids[:i]:
                    numbered = re.match(r"^s\d+$", to)
                    out.append(
                        _issue(
                            "error",
                            i,
                            s,
                            (
                                (
                                    "back_to %r — stops are numbered s1, s2… in order; "
                                    "name one before the Loop (it is %s)" % (to, ids[i])
                                )
                                if numbered
                                else (
                                    "back_to %r is no stop of this route — use the id "
                                    "of an earlier stop (s1…) or 'agent'" % to
                                )
                            ),
                            "steps[%d].config.back_to" % i,
                        )
                    )
            if i == 0 and to != "agent":
                out.append(
                    _issue(
                        "error",
                        i,
                        s,
                        "a Loop first has nothing to repeat — put it after the "
                        "stops it repeats (or back_to='agent')",
                    )
                )
            times = _int(c.get("times"), 3)
            if times == 0 and not c.get("until"):
                out.append(
                    _issue(
                        "warn",
                        i,
                        s,
                        "times=0 with no until loops forever — set times or until",
                    )
                )
            if to == "agent" and not on_session:
                out.append(
                    _issue(
                        "warn",
                        i,
                        s,
                        "Loop back to the agent needs a session (a route in a "
                        "folder has no agent)",
                    )
                )
        elif kind in ("command", "preview") and not str(c.get("command") or "").strip():
            out.append(
                _issue(
                    "error",
                    i,
                    s,
                    "a %s stop needs config.command — ask the person for it, "
                    "never invent one" % kind,
                )
            )
        elif kind == "call" and not str(c.get("url") or "").strip():
            out.append(
                _issue("error", i, s, "a Call needs config.url — ask the person for it")
            )
        elif kind == "screenshot":
            urls = str(c.get("urls") or "{preview}")
            if "{preview}" in urls and "preview" not in before:
                out.append(
                    _issue(
                        "warn",
                        i,
                        s,
                        "Screenshot of {preview} with no Preview before it — add a "
                        "preview stop or give config.urls",
                    )
                )
        when = str(s.get("when") or "")
        if re.search(r"\bjudge\b", when, re.I) and "judge" not in before:
            out.append(
                _issue(
                    "error",
                    i,
                    s,
                    "when %r reads a Judge but none comes before — add a judge "
                    "stop earlier" % when,
                )
            )
        until = str(c.get("until") or "") if kind == "loop" else ""
        if re.search(r"\bjudge\b", until, re.I) and "judge" not in before:
            out.append(
                _issue(
                    "error",
                    i,
                    s,
                    "until %r reads a Judge but none comes before the Loop — add "
                    "a judge stop before it" % until,
                )
            )
    if renumber is not None:
        for x in out:
            x["stop"] = ids[x["at"]]
    return out


def errors(issues: Sequence[dict]) -> List[dict]:
    return [x for x in issues if x.get("level") == "error"]


def issue_line(x: dict) -> str:
    return "%s: %s" % (x.get("stop") or "route", x.get("text"))


#: ``steps[2].config.lane`` → the server's refusal, taught.
_FIELD_RE = re.compile(r"^steps\[(\d+)\](?:\.(\w+))?(?:\.(\w+))?")


def field_hint(field: str, detail: str = "", known: Iterable[str] = ()) -> str:
    """What to do about a server refusal (``400 bad_step`` ``{detail,
    field}``), ``""`` when there is nothing to add."""
    m = _FIELD_RE.match(str(field or ""))
    if not m:
        return ""
    part, sub = m.group(2) or "", m.group(3) or ""
    d = str(detail or "")
    if part == "kind":
        if "Finish" in d:
            return "move the Finish (land) stop to the end"
        km = re.search(r"kind '([^']*)'", d)
        hint = closest_kind(km.group(1) if km else "", known or STOPS)
        return "use %s" % hint if hint else "pick a kind from describe_flight's stops"
    if part == "when" or sub == "until":
        return (
            "conditions: diff touches <glob>, diff lines > N, files < N, weekday, "
            "hour between H and H, branch matches <glob>, label <name>, judge is "
            "<value>, always, never (and / or / not)"
        )
    if part == "parallel":
        return "only robot stops that don't use the agent run side by side; use a split for agents"
    if part == "on_fail":
        return "on_fail is halt, agent, skip or you"
    if part == "config" and sub:
        return "fix config.%s (describe_flight lists each stop's fields)" % sub
    return ""


# --------------------------------------------------------------------------- #
# The one-shot prompt's half (flight_describe.py)
# --------------------------------------------------------------------------- #
def prompt_text() -> str:
    """When to use each kind, the rules and the recipes, for the one-shot
    model — the same words the MCP agent gets."""
    lines = ["When each kind fits (RARE = only when the request names it):"]
    for kind in STOPS:
        lines.append("- " + stop_line(kind))
    lines.append("")
    lines.append("Rules:")
    lines.extend("- " + r for r in RULES)
    lines.append("")
    lines.append("Common routes (copy and adapt):")
    for r in RECIPES:
        lines.append(
            "- %s: %s" % (r["when"], json.dumps(r["steps"], separators=(",", ":")))
        )
    return "\n".join(lines)
