"""The impure half of Verify (manual test plans): trigger, watch, run, sweep.

:mod:`backend.web.core.test_plans` holds the store, the prompt building and the
generation one-shot. This module is the driver around it:

* the trigger — :func:`_ensure_test_plan`, fired by the ``session.pushed`` bus
  subscriber below (and from the server's state tick), which writes a plan in
  the background;
* the due loop — :func:`_test_plans_due_loop` / :func:`_test_plans_due_pass`,
  which watches origin until the work is really live, polls running verify
  sessions for their results file, refreshes where a change has landed and
  sweeps orphaned verify sessions;
* the helpers the run / fix / regenerate / write routes share (worktree
  reclaim, the closed-session inputs, ending a verify session).

The routes stay in ``server.py`` and call in here. Anything server-owned
(``ENGINE``, ``close_instance``, the probes tests patch on ``server``) is
reached lazily through :func:`_server`, and server.py re-imports every name
defined here, so ``server._ensure_test_plan`` and friends keep resolving.
"""

from __future__ import annotations

import asyncio
import datetime as _datetime
import json
import os
import subprocess
import threading
import time
from typing import Dict, Optional, Tuple

from fastapi.responses import JSONResponse

from backend import gitutil, log
from backend.session import tmux
from backend.session.storage import Loading, Paused
from backend.web.core import events as _events
from backend.web.core import hidden_sessions as _hidden_sessions
from backend.web.core import ntfy as _ntfy
from backend.web.core import prompt_queue as _prompt_queue
from backend.web.core import test_plans as _test_plans
from backend.web.core import worktree_setup as _wt_setup


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import)."""
    from backend.web import server

    return server


# --- Verify: manual test plans for work that has gone live -------------------- #
# The impure half of :mod:`backend.web.core.test_plans` (the store, the prompt
# building and the generation one-shot live there; the routes are in server.py).
# Three things live apart from the store because they need the server: the
# trigger that fires off a push, the loop that watches origin until the work is
# really live, and the helpers behind the run route's use of ``create_instance``.
#
# The whole feature hangs off ONE entry point, :func:`_ensure_test_plan`, called
# from two independent triggers on purpose — see the comment in
# :func:`_emit_state_changes` for why the redundancy is required and why it is
# free.

#: The due loop's cadence. A minute is deliberately unhurried: this waits for a
#: PR to merge, which happens on human time, and every pass costs a ``git fetch``
#: per waiting plan. Nothing in the product degrades if the "it's live" card
#: appears 59 seconds late.
_TEST_PLAN_DUE_INTERVAL = 60.0

#: The cadence while a verify session is actually running. A minute is right for
#: waiting on a merge and badly wrong for waiting on an agent that is finishing
#: on the user's own screen: the run writes its results file, the session goes
#: quiet, and the row goes on saying "an agent is checking the steps it can" for
#: up to another minute — during which the panel's Refresh button, which on every
#: other surface in the app means "go and ask again", cannot change the answer
#: either, because the only thing that reads that file is this loop. So while
#: anything is running the loop drops to a few seconds. It costs nothing: the
#: running phase is purely local (one ``open()`` per running plan), the expensive
#: half stays on its own minute cadence (see ``full`` in
#: :func:`_test_plans_due_pass`), and there is normally nothing running at all.
_TEST_PLAN_RUN_POLL_S = 4.0

#: How long a verify session may sit in ``running`` before the plan is handed
#: back to the user. A run that has not written its results file in two hours is
#: not slow, it is wedged — the agent crashed, the user took the session over and
#: forgot, the CLI hit a usage limit and stopped. Releasing the plan to ``due``
#: is strictly better than leaving it in a state whose only exit is a file that
#: is never going to be written; the user can re-run it, or answer the steps by
#: hand.
_TEST_PLAN_RUN_GIVE_UP_S = 2 * 60 * 60.0

#: How long ONE pass may spend asking origin whether waiting plans have gone
#: live. Half the interval, so a pass normally finishes inside its own tick.
#: The per-call caps in ``test_plans`` (120s for the fetch) stop a single
#: unreachable remote from wedging the loop, but they are per CALL: a flock with
#: a dozen branches waiting to ship, on a laptop whose VPN just dropped so the
#: origin host blackholes TCP rather than refusing, pays that timeout once per
#: plan and the pass runs for half an hour. This is the aggregate cap the
#: per-call ones cannot be. Checked BETWEEN plans, so a pass can overrun by at
#: most one plan's worth of timeouts.
_TEST_PLAN_LIVE_BUDGET_S = 30.0

#: plan id -> epoch when its liveness was last asked. The cursor that makes the
#: budget above fair rather than a guillotine: see :func:`_liveness_order`. In
#: memory on purpose — it is a scheduling hint, and a restart re-asking every
#: plan once is exactly the right behaviour.
_TEST_PLAN_LIVE_CHECKED: Dict[str, float] = {}

#: How long a plan's "where has this landed" answer is good for, and how long
#: ONE pass may spend refreshing them. Minutes rather than the liveness pass's
#: every-tick rotation, because this is a fact somebody READS off a card rather
#: than one the machine acts on: nothing is marked due by it, nothing is pushed
#: to a phone, and a branch name that is five minutes stale costs nobody
#: anything. The cheap half is local (one `for-each-ref` per plan); the fetch it
#: rides on is shared per repository — see ``test_plans.fetch_all_heads``.
_TEST_PLAN_LANDED_TTL_S = 300.0
_TEST_PLAN_LANDED_BUDGET_S = 15.0

#: How long after its most recent landing a plan is still asked "and now?".
#: The stop condition for the landing pass, and the reason it is a CLOCK rather
#: than a branch name is in :func:`_check_test_plan_landings`: the branch a
#: change sits on climbs (merged into staging on Tuesday, promoted into main on
#: Wednesday) and no branch name marks the top of that climb, but a promotion
#: that has not happened within a week is not coming. Generous on purpose — the
#: cost of a too-long window is one cheap local probe per plan per five minutes,
#: while a too-short one is a wrong branch name on the card, which is the bug
#: this window exists to end.
_TEST_PLAN_LANDED_SETTLE_S = 7 * 86400.0

#: plan id -> epoch when its landing was last asked. Same cursor trick as
#: :data:`_TEST_PLAN_LIVE_CHECKED`, and in memory for the same reason.
_TEST_PLAN_LANDED_CHECKED: Dict[str, float] = {}


def _verify_enabled() -> bool:
    """The Verify master switch (``repository.verify_enabled``).

    Read fresh rather than cached: it is a switch a person flips expecting the
    next minute's loop to obey it, and ``load_settings`` is already the cheap,
    memoized read every other setting on this path goes through.

    Local import for the reason the module docstring gives and
    ``server-settings-local-import-trap`` learned the hard way: a bare
    ``load_settings()`` here would NameError into the house try/except and
    silently answer "off" forever.

    Fails OPEN. A settings file this cannot read is not a decision to pause —
    defaulting to off would silently stop writing plans for a user who never
    touched the switch, and the failure would look exactly like the feature
    being broken.
    """
    try:
        from backend.config import settings as _settings

        return bool(_settings.load_settings().repository.verify_enabled)
    except Exception:  # noqa: BLE001
        return True


def _verify_auto_for(repo_root: str, wt: str) -> bool:
    """Whether a push in this repo should write a plan by itself.

    The master switch outranks both opt-ins below. It is not a third opt-in but
    a pause over the whole feature: a user who switches Verify off means "stop
    doing this on your own", and a repo's committed ``verify_on_push`` — which
    is a statement about the repo, not about this machine — must not be able to
    override that. Explicit requests (Write plan, Run) are unaffected; see
    ``RepositorySettings.verify_enabled``.

    Two independent opt-ins, OR'd, because they answer different questions. The
    repo's committed ``.mindflock.toml`` says "everyone who clones this should
    get plans" and travels with the code; the list half says "a person typed
    this repo into the Verify dialog on this machine" and never touches a
    tracked file. Neither can switch the other off — a local list that could
    silently override a repo's committed intent (or vice versa) would make both
    untrustworthy.

    MEMBERSHIP IS THE LOCAL OPT-IN. There is no per-repo ``auto`` flag any more:
    a repo gets automatic plans because somebody added it to
    ``repository.verify_repos``, exactly as a repo in ``github.repos`` gets its
    PRs reviewed by virtue of being there. The match is by the ``owner/name``
    behind this checkout's ``origin`` rather than by its path, so one entry
    covers every clone and every worktree of that repo — see
    :func:`test_plans.is_tracked`, which never raises and whose slug lookup is
    memoized, so this costs at most one ``git remote get-url`` per repo per
    minute on the push path.

    The file half is not legacy. It is the ONLY opt-in available to a checkout
    with no GitHub origin — a local-path remote (MindFlock's own provisioned
    clones are exactly that), another forge, or no remote at all — because such
    a repo has no slug to be listed under.

    The ``.mindflock.toml`` is read from ``wt``, the worktree the push came out
    of: that is the checkout whose branch is being verified, and its copy of the
    file is the one that shipped with this branch.
    """
    if not _verify_enabled():
        return False
    try:
        if _wt_setup.load_config(wt).verify_on_push:
            return True
    except Exception:  # noqa: BLE001 — a malformed toml is not an opt-in
        pass
    return _test_plans.is_tracked(repo_root)


def _ensure_test_plan(title: str, manual: bool = False) -> None:
    """Idempotent: create + generate a test plan for a session that just landed
    on origin. **Never raises, never blocks the caller.**

    ``manual`` is the button saying a person asked for this plan by name, and it
    skips the repo's ``verify_on_push`` opt-in (see
    :func:`_ensure_test_plan_blocking`). Nothing else differs: an explicit
    request and an opted-in push produce exactly the same plan, so there is one
    generation path and not two.

    Both triggers (the ``session.pushed`` subscriber and the stage-transition
    fallback) run on a worker thread inside an event emit, and one of them is on
    the path of the state tick that every client's poll depends on. So this
    function does no work at all itself: it hands the whole job — the git
    probes, the store write, and the up-to-``TIMEOUT_GENERATE`` model call — to
    its own daemon thread and returns immediately.

    A daemon thread rather than ``_register_task(asyncio.to_thread(...))``
    because there is no running loop to create a task on: ``_announce_push``
    reaches us through ``asyncio.to_thread`` and ``_emit_state_changes`` through
    the instances tick, both of which are worker threads where
    ``asyncio.create_task`` raises. (This is the same trap ``live_stage.watch``
    documents; here there is nothing to hop back to the loop FOR, so the thread
    just does the work.)

    Racing callers are not a problem and are not guarded against here:
    ``ensure_plan_for`` decides under its own lock and returns ``None`` to
    everyone but the first, so a duplicate trigger costs one store read.
    """
    if not title:
        return
    try:
        threading.Thread(
            target=_ensure_test_plan_blocking,
            args=(title, manual),
            name="mf-testplan-%s" % title[:48],
            daemon=True,
        ).start()
    except Exception:  # noqa: BLE001 — a plan we could not start is not a reason
        # to fail the push that triggered it. The next push tries again.
        pass


def _ensure_test_plan_blocking(title: str, manual: bool = False) -> None:
    """The body of :func:`_ensure_test_plan`, on its own thread.

    Resolves everything the plan must outlive its session with, then generates.
    The one subtle field is ``repo_root``: it is the MAIN repo
    (``GetGitWorktree().GetRepoPath()``, the same accessor
    ``core.workspaces._base_clone_references`` uses to answer "which repo is this
    session's"), never the worktree. A plan comes due when the work merges, by
    which time the session is normally deleted and its worktree reclaimed — a
    plan pointing at the worktree would be a plan that can never be run, and the
    due loop's ``git fetch`` would have nowhere to run either. The worktree is
    still passed to ``generate`` as its cwd, because right now — seconds after
    the push — it is the only place the branch's diff is guaranteed to be
    readable.
    """
    srv = _server()
    try:
        inst = srv.ENGINE.instances.get(title)
        if inst is None:
            return  # deleted between the push and this thread starting
        wt = inst.GetWorktreePath()
        if not wt:
            return  # a session with no git workspace has nothing to verify
        try:
            repo_root = inst.GetGitWorktree().GetRepoPath() or ""
        except Exception:  # noqa: BLE001 — raises when Start hasn't finished
            repo_root = ""
        # In-place sessions have repoPath == worktreePath, so this fallback only
        # ever fires for a session whose worktree object is not readable yet —
        # and then the worktree path is the best (and correct) guess.
        repo_root = repo_root or wt
        # Explicit opt-in, exactly like the O3 check gate two fields up in the
        # same file: a repo that has not asked for this gets NOTHING on a push.
        # Generating a plan is a real model call of up to TIMEOUT_GENERATE, and
        # a flock pushing across several repos all day would spend it on plans
        # nobody asked for — and worse, fill the Verify badge, which only means
        # anything while it counts things a person actually intends to check.
        # The button on a pushed session (POST /api/instances/{title}/test-plan,
        # which arrives here with manual=True) is the normal way to get a plan;
        # the opt-in is for a repo whose every change warrants a manual test.
        if not manual and not _verify_auto_for(repo_root, wt):
            return
        branch = srv._current_branch(wt)
        sha = srv._git_head_sha(wt)
        # ``repo_root``, not nothing: the live branch is a PER-REPO fact, and the
        # plan is stamped with whichever branch it is told at creation — it is
        # what the due loop watches and what the run prompt checks out. Asking
        # for the flock-wide default here would silently ignore this repo's own
        # override, i.e. the whole feature not working for exactly the repo the
        # user bothered to configure.
        plan = _test_plans.ensure_plan_for(
            title,
            branch,
            sha,
            repo_root,
            _test_plans.resolve_live_branch(repo_root),
            intent=_test_plan_intent(title),
        )
        if plan is None:
            # This branch already has a plan — ``ensure_plan_for``'s idempotent
            # no-op, unchanged: five pushes still make ONE plan. What a later
            # push MAY do is refresh that one plan, and only while nobody has
            # answered anything on it. Deliberately after the ``_verify_auto_for``
            # gate above, so a repo nobody tracks still gets nothing.
            #
            # ``owner`` rather than ``title``, because the plan for this branch
            # may belong to ANOTHER window on it (a duplicated window, a repo
            # adopted twice — see ``test_plans._branch_owner``). Refreshing
            # under this session's title would find nothing and quietly stop
            # recording the new tip, so the checklist for a branch two windows
            # share would freeze at whichever push the first window saw.
            owner = _test_plans.owner_for_branch(title, repo_root, branch)
            if owner and _test_plans.refresh_for_push(owner, branch, sha) is not None:
                _generate_test_plan(
                    owner, getattr(inst, "Program", "") or "", wt, refresh=True
                )
            return
        _generate_test_plan(title, getattr(inst, "Program", "") or "", wt)
    except Exception as err:  # noqa: BLE001 — nothing above may surface: this
        # thread has no caller left to raise into, and a missing test plan must
        # never look like a failed push.
        if log.ErrorLog is not None:
            try:
                log.ErrorLog.Printf("test plan for %s could not start: %v", title, err)
            except Exception:  # noqa: BLE001
                pass


def _generate_test_plan(
    plan_id: str, program: str, worktree: str, refresh: bool = False
) -> None:
    """Run the generation one-shot and announce the result. Blocking (up to
    ``test_plans.TIMEOUT_GENERATE``); call it on a thread.

    ``generate`` never raises for a *generation* failure — a timeout, a CLI with
    no headless mode, an unparseable answer all land in the plan as
    ``state="failed"`` with a sentence for the user — so the only thing left to
    do here is emit when it actually worked. ``session.test_plan_ready`` is
    deliberately NOT emitted for a failure: the event means "there are steps
    worth showing", and the Verify dialog re-fetches on it.

    The wrapper is for the failures ``generate`` can't own — a store that cannot
    be written, a disk that filled. This is a thread ENTRY POINT (see
    :func:`_start_test_plan_generation`), and an escaping exception there is a
    bare traceback on stderr with nobody's request attached to it.
    """
    try:
        plan = _test_plans.generate(plan_id, program, worktree, refresh) or {}
    except Exception as err:  # noqa: BLE001
        if log.ErrorLog is not None:
            try:
                log.ErrorLog.Printf("test plan %s generation failed: %v", plan_id, err)
            except Exception:  # noqa: BLE001
                pass
        return
    if plan.get("state") != "generated":
        # NOT SILENCE. `test_plan_ready` keeps its narrow meaning — there are
        # steps worth showing, which is what the dialog refetches on — but a
        # rewrite that failed used to emit nothing at all, so the row went on
        # saying "writing…" for up to a poll and then changed with no
        # explanation. A sibling event says what happened.
        if plan.get("error"):
            _events.BUS.emit(
                "session.test_plan_failed",
                session=plan_id,
                data={
                    "plan": plan_id,
                    "error": str(plan.get("error") or ""),
                    "refreshed": refresh,
                },
            )
        return
    _events.BUS.emit(
        "session.test_plan_ready",
        session=plan_id,
        # ``refreshed`` is additive: the Verify dialog already refetches on this
        # event, so nothing in the frontend has to change to see a refreshed
        # checklist — the flag is for anyone reading the bus.
        data={
            "plan": plan_id,
            "steps": len(plan.get("steps") or []),
            "refreshed": refresh,
        },
    )


#: How many generation attempts a plan gets before the due loop stops trying and
#: parks it in ``failed``. Two, i.e. the original plus exactly one automatic
#: retry: the ordinary cause of a stall is the app closing mid-write, which the
#: retry fixes outright, while a machine where generation reliably dies (a CLI
#: that hangs, a repo that is gone) must reach a sentence a person can read
#: instead of re-spending a model call every five minutes forever.
_TEST_PLAN_GEN_ATTEMPTS = 2


def _test_plan_intent(plan_id: str) -> str:
    """What this session was ASKED to do, from wherever it still survives.

    Called at the one moment the answer is knowable — while the session that did
    the work is still alive — so ``test_plans`` can write it onto the plan and
    stop depending on the engine forever after. See the ``intent`` field in
    ``test_plans._blank``.

    THREE SOURCES, and the second one is not a nicety. ``create_instance`` blanks
    ``inst.Prompt`` and hands the text to the prompt queue whenever a repo has
    committed ``[workspace]`` setup commands (see the hold above) — so reading
    only ``Prompt`` loses the ticket entirely for exactly the repos whose owners
    configured them hardest. The third is the transcript: a session whose prompt
    was delivered and consumed still has it written down in its own scrollback.
    """
    inst = _server().ENGINE.instances.get(plan_id)
    if inst is None:
        return ""
    seed = str(getattr(inst, "Prompt", "") or "").strip()
    if not seed:
        try:
            queued = _prompt_queue.list_queue(plan_id) or []
            seed = str((queued[0] or {}).get("text") or "").strip() if queued else ""
        except Exception:  # noqa: BLE001 — best effort, never a failed push
            seed = ""
    if not seed:
        try:
            from backend.web.core.agent_state import _session_find_prompt

            seed = _session_find_prompt(inst, "# Story:") or ""
        except Exception:  # noqa: BLE001
            seed = ""
    return _test_plans.intent_from_prompt(seed)


def _test_plan_session_ctx(plan_id: str) -> tuple:
    """``(program, worktree)`` for generating this plan, from its session.

    Plans are keyed by session title, so the session — when it still exists — is
    where the two things generation wants come from: the CLI that answers the
    question, and the checkout whose branch the diff is readable in. Both are
    optional: ``generate`` falls back to the flock's default program and to the
    plan's ``repo_root``, which is what lets a plan be rewritten long after its
    session was deleted.

    Shared by the regenerate route and the stall recovery below so the two paths
    cannot drift into asking for different trees.
    """
    inst = _server().ENGINE.instances.get(plan_id)
    if inst is None:
        return "", ""
    try:
        worktree = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001 — a session mid-Start has no worktree yet
        worktree = ""
    return getattr(inst, "Program", "") or "", worktree


def _recover_stalled_test_plans(plans: list) -> None:
    """Pick generations back up that nothing is going to finish.

    THE BUG THIS ENDS: generation runs on a daemon thread, so quitting the app
    while a plan is being written kills it mid-answer. Nothing else ever writes
    that plan again — ``generating`` is the one state whose every exit is written
    by the thread that just died — so the card read "Writing the plan from the
    diff — up to three minutes" forever, and the dialog hides the rewrite button
    in that state, so there was no way out of it from inside the product.

    The loop that already exists is the right owner: it wakes every minute, it
    runs its first pass at startup (which is precisely when the abandoned plans
    are), and it is already the place where "a plan is stuck in a state nothing
    will move it out of" is handled for running plans — see
    ``_TEST_PLAN_RUN_GIVE_UP_S``. This is the same watchdog one rung up the
    ladder.

    Retry first, give up second. A stall is usually the app having been closed,
    and the honest fix for that is to write the plan the user is waiting for, not
    to show them an error about a machine that is no longer switched off.
    :data:`_TEST_PLAN_GEN_ATTEMPTS` bounds it, and ``test_plans.is_stalled``
    guarantees we never race a generation that is merely slow.

    Skipped entirely while Verify is paused, exactly like the liveness phase: the
    switch means the feature is quiet, and a paused Verify that fired model calls
    on a timer would be the loudest thing it does. Nothing is lost — a stalled
    plan keeps its state and the next enabled pass recovers it.
    """
    if not _verify_enabled():
        return
    for plan in plans:
        try:
            if not _test_plans.is_stalled(plan):
                continue
            pid = plan["id"]
            if int(plan.get("gen_attempts") or 0) >= _TEST_PLAN_GEN_ATTEMPTS:
                if _test_plans.give_up_generating(pid) is not None and (
                    log.ErrorLog is not None
                ):
                    try:
                        log.ErrorLog.Printf(
                            "test plan %s: generation stalled twice, giving up", pid
                        )
                    except Exception:  # noqa: BLE001
                        pass
                continue
            program, worktree = _test_plan_session_ctx(pid)
            # A plan that already HAS steps is being refreshed, whatever
            # started the generation that stalled — so a second failure must put
            # it back in ``generated`` rather than parking a working checklist in
            # ``failed`` and taking it out of the due loop.
            _start_test_plan_generation(
                pid, program, worktree, refresh=bool(plan.get("steps"))
            )
        except Exception:  # noqa: BLE001 — one bad plan can't stop the pass
            pass


def _retry_no_commit_test_plans(plans: list) -> None:
    """Write the checklists that couldn't be written for want of a commit, once
    their session has committed.

    The card says "Comes back automatically when the session commits", and
    this is what keeps that true: a ``failed`` plan is otherwise never retried
    (``ensure_plan_for`` treats it as settled). Only a plan whose session is
    still open is looked at — its worktree is where the new commit is — and
    only a NEW commit claims it (:func:`test_plans.claim_no_commits_retry`), so
    a branch that is still empty costs one ``rev-list`` a minute and no model
    call. Quiet while Verify is paused, like the stall recovery above."""
    if not _verify_enabled():
        return
    for plan in plans:
        try:
            if plan.get("state") != "failed" or plan.get("steps"):
                continue
            if not _test_plans.is_no_commits_error(plan.get("error") or ""):
                continue
            pid = plan["id"]
            program, worktree = _test_plan_session_ctx(pid)
            if not worktree or not os.path.isdir(worktree):
                continue
            live = plan.get("live_branch") or _test_plans.resolve_live_branch(
                plan.get("repo_root") or worktree
            )
            if _test_plans.branch_has_no_commits(worktree, live):
                continue
            head = _server()._git_head_sha(worktree)
            if head and _test_plans.claim_no_commits_retry(pid, head) is not None:
                _start_test_plan_generation(pid, program, worktree)
        except Exception:  # noqa: BLE001 — one bad plan can't stop the pass
            pass


def _start_test_plan_generation(
    plan_id: str, program: str, worktree: str, refresh: bool = False
) -> None:
    """Fire :func:`_generate_test_plan` on a daemon thread and return at once.

    The regenerate route's spawner. Same reasoning as :func:`_ensure_test_plan`'s
    thread: three minutes is far too long for a request (or for anything else
    sharing the event loop) to wait on, and the answer is written to the store,
    so nobody has to be listening when it lands.
    """
    try:
        threading.Thread(
            target=_generate_test_plan,
            args=(plan_id, program, worktree, refresh),
            name="mf-testplan-%s" % plan_id[:48],
            daemon=True,
        ).start()
    except Exception:  # noqa: BLE001 — the plan keeps whatever it already had
        pass


def _on_session_pushed(envelope: dict) -> None:
    """Bus subscriber: a session's branch reached origin, so it needs a plan.

    The PRIMARY trigger. ``live_stage`` emits ``session.pushed`` exactly once per
    watcher, at the instant the remote branch head equals the local one — the
    only moment in the whole process where "that sha is on origin" is a known
    fact rather than an inference. Subscribers run synchronously on the emitting
    thread, so this must return immediately; :func:`_ensure_test_plan` does.
    """
    try:
        if envelope.get("event") != "session.pushed":
            return
        _ensure_test_plan(str(envelope.get("session") or ""))
    except Exception:  # noqa: BLE001 — a subscriber that raises is logged by the
        # bus and skipped, but a push watcher is not the place to find that out.
        pass


# Subscribed at import, NOT in the lifespan, because the bus is process-wide
# while the lifespan is per-ASGI-app: a test (or the CLI) that imports this
# module and drives the engine directly still gets the trigger, and there is
# nothing to tear down — the callback outlives nothing.
_events.BUS.subscribe(_on_session_pushed)


def _merging_is_shipping(live_branch: str) -> bool:
    """Whether, in this flock, "the PR merged" is evidence that work SHIPPED.

    The gate on the squash-merge fallback below, and the reason
    ``repository.live_branch`` exists as its own knob. In most repos a PR merges
    into the branch users get and the two questions are the same one. In a shop
    that PRs into ``develop`` and ships from ``release`` — the exact split the
    setting's own docstring describes — they are not: a merged PR there says the
    work reached ``develop``, which is not what a Verify plan is waiting for.

    ``pr_base_branch or base_branch or "main"`` mirrors
    ``test_plans.resolve_live_branch``'s chain with both of its live-branch links
    removed (this repo's own override and the flock-wide one): it is where a PR
    lands when nobody says otherwise. Equal to the plan's live branch
    means merging IS shipping and a MERGED PR may stand in for ancestry;
    different means it may not, and the plan waits for the real ancestry test
    (which is what "live" means and is never wrong, only sometimes silent).

    THE QUESTION IS ABOUT THE FLOCK'S SHAPE, NOT ABOUT ONE BRANCH NAME, and that
    is why the flock-wide live branch is asked for as well. There is no per-repo
    PR base anywhere — ``verify_repo_settings`` carries ``live_branch`` and
    ``prompt`` and nothing else — so comparing a PER-REPO live branch against the
    FLOCK-WIDE PR target compares two things that were never about the same
    repo. A repo whose
    card says it ships from ``staging`` while the flock configures no PR base at
    all would score ``"main" != "staging"`` and lose the squash-merge fallback
    forever: its plans would sit in ``generated`` and never come due, i.e. the
    feature would break for exactly the repo somebody bothered to configure, and
    would have worked before they configured it. So the split is detected once,
    flock-wide — does the PR target differ from what the flock calls live? — and
    only a flock that really has typed that split (``pr_base_branch=develop``
    with ``live_branch=release``) falls through to the per-plan comparison, which
    still lets a plan whose branch IS the PR target keep the fallback.

    Unreadable settings answer ``True``, i.e. the pre-existing behaviour: the
    divergent configuration is one the user had to type, and a flock that never
    touched the setting must not lose the squash-merge fallback over a failed
    settings read.
    """
    try:
        # Function-local by house rule (see :func:`_configured_pr_base`): a
        # module-level-only reference NameErrors into the except below and
        # silently answers the fallback forever.
        from backend.config.settings import load_settings

        r = load_settings().repository
        target = (r.pr_base_branch or r.base_branch or "main").strip()
    except Exception:  # noqa: BLE001
        return True
    # No repo argument on purpose: this is the flock-wide answer, the one thing
    # ``target`` is comparable with. When they agree, nobody has declared a
    # PR-base/ship split and merging is shipping everywhere in the flock —
    # including in a repo that overrode its own live branch, whose override says
    # where IT ships and says nothing about a develop/release process.
    if target == _test_plans.resolve_live_branch():
        return True
    return target == str(live_branch or "").strip()


def _test_plan_is_live(plan: dict) -> bool:
    """Whether this plan's work has reached the live branch. Blocking (a fetch
    plus, sometimes, a PR lookup) — call it on a thread.

    Two questions, because one of them cannot answer on its own. Ancestry
    (``test_plans.is_live``) is the honest test and the only one that works for a
    repo with no PRs at all. But a **squash merge rewrites the commit**, so the
    sha this plan recorded at push time never becomes an ancestor of anything —
    for the many flocks that squash by default, ancestry alone would mean no plan
    is EVER due, which is the feature silently not existing. So a branch whose
    most recent PR reports ``MERGED`` counts as live too — but only where merging
    IS shipping (:func:`_merging_is_shipping`).

    That gate is not a nicety. ``_pr_info`` matches by head branch alone and is
    deliberately NOT filtered by base (its docstring says why, and it cannot be:
    neither rung even returns the base a PR merged into). So without the gate,
    the fallback reads "this branch's PR merged into *something*" — and in a shop
    that PRs into ``develop`` and ships from ``release`` that fires on every
    merge, marking plans due, pushing "it's live — verify it" to a phone, and
    sending a verify run at a branch that does not contain the change. Worse, it
    is a one-way door: liveness is only re-asked while the plan is ``generated``,
    so the premature ``due`` is never corrected when the work really ships.

    ``_pr_info`` is reused rather than re-asked: it is the same memoized, sticky,
    gh-or-REST lookup the stage machine runs, so the two can never disagree about
    whether a branch's PR merged. It is given the MAIN repo as its cwd — the
    worktree it was written for is usually gone by now, and the main repo has the
    same origin, which is all ``gh`` needs.
    """
    verdict = _test_plans.probe_live(
        plan["repo_root"], plan["sha"], plan["live_branch"]
    )
    if verdict == "live":
        _test_plans.set_live_problem(plan["id"], "")
        return True
    # SAY WHEN THE WAIT CANNOT END. "Not shipped yet" and "waiting for a branch
    # origin does not have" look identical on screen — a row cheerfully saying
    # "it turns up here to check when it ships" — and only one of them is the
    # user's to fix. A checklist that can never come due is worse than no
    # checklist, because the whole promise is that it tells you.
    if verdict == "missing":
        _test_plans.set_live_problem(
            plan["id"],
            "Origin has no branch called %s, so this can never come due — set "
            "the live branch under Edit repositories." % (plan["live_branch"] or "?"),
        )
    else:
        # "waiting" and "unreachable" are both ordinary; clear any stale
        # diagnosis (the branch may have just been created, or the network came
        # back) rather than leaving a sentence that is no longer true.
        _test_plans.set_live_problem(plan["id"], "")
    info = _server()._pr_info(plan["repo_root"], plan["branch"])
    merged = bool(info) and str(info.get("state") or "").upper() == "MERGED"
    base = str((info or {}).get("base") or "").strip()
    live = str(plan["live_branch"] or "").strip()
    if merged and base:
        # THE EXACT ANSWER, when the PR can give one. A squash merge rewrites the
        # commit, so "this branch's PR merged" is the only evidence left that the
        # work shipped — and it is good evidence precisely when the PR merged
        # into the branch this checklist is waiting for. That is a fact about the
        # PR, and asking it beats inferring it from flock-wide settings, which is
        # what `_merging_is_shipping` has to do when the base is unknown.
        if base == live:
            _test_plans.set_live_problem(plan["id"], "")
            return True
        # Merged, but somewhere else. This is the state that used to wait for
        # ever in silence: a repo that PRs into `staging` and ships from `main`
        # has work that is genuinely merged and genuinely not live, and the row
        # said "it turns up here to check when it ships" indefinitely. Say what
        # happened, and let the deploy question stay open.
        _test_plans.set_live_problem(
            plan["id"],
            "Its pull request merged into %s, not %s. This checklist is waiting "
            "for %s \u2014 change the live branch on this repo's card if %s is "
            "what you ship." % (base, live, live, base),
        )
        return False
    if not _merging_is_shipping(live):
        return False
    return merged


def _test_plan_merged_into(plan: dict) -> dict:
    """Where this plan's work has reached on origin. Blocking — call it on a
    thread. Shaped like ``test_plans.probe_merged_into``.

    THE QUESTION THE CARD COULD NOT ANSWER. A checklist knows the branch it was
    pushed on and the branch it is waiting for; between those two it says nothing
    about the branch the work is actually sitting on right now, which in a repo
    with a develop or a release step is most of a change's life. "Is this in
    staging yet, or already in main?" is the thing somebody scanning the list
    wants, and until this it was a question you answered by leaving the app.

    Two rungs, the same two — and in the same order — as :func:`_test_plan_is_live`:

    * **Ancestry** (``probe_merged_into``) is the honest test, works in a repo
      with no PRs at all, and is the only one that can name a branch nobody
      opened a PR against.
    * **The PR's own base**, when ancestry finds nothing. A SQUASH merge rewrites
      the commit, so the sha this plan recorded never becomes an ancestor of
      anything and ancestry says "nowhere" about work that demonstrably shipped —
      for a flock that squashes by default, that is the feature silently not
      existing. ``_pr_info`` is reused rather than re-asked, so this and the due
      loop can never disagree about where a branch's PR went.

    Unlike the liveness fallback this one needs no ``_merging_is_shipping`` gate:
    it is not deciding whether anything shipped, only reporting the branch a
    merged PR names as its base. A PR that merged into ``develop`` is exactly the
    case this is here to show.
    """
    found = _test_plans.probe_merged_into(
        plan.get("repo_root") or "", plan.get("sha") or "", plan.get("branch") or ""
    )
    if found.get("branch"):
        return found
    info = _server()._pr_info(plan.get("repo_root") or "", plan.get("branch") or "")
    if not info or str(info.get("state") or "").upper() != "MERGED":
        return found
    base = str(info.get("base") or "").strip()
    if not base:
        return found
    # The PR knows WHERE but this rung cannot know WHEN — `_pr_info` does not
    # carry a merge time. `merged_at` is the closest honest stamp (the moment
    # this flock first saw the work merged) and 0.0 says "no idea", which is
    # what the row renders as an unqualified "merged into develop".
    return {
        "branch": base,
        "at": float(plan.get("merged_at") or 0.0),
        "all": [base],
    }


def _notify_test_plan_due(plan: dict) -> None:
    """Push "this is live, go check it" to the user's phone, when ntfy is on.

    AT MOST ONCE PER PLAN — see ``test_plans.mark_notified``. Several things can
    move a plan to ``due`` more than once (pressing "it's out, check it now", a
    rewrite of a plan that had already shipped), and *"sc-1234 shipped to main"*
    arriving days after it shipped is a notification that is not true, about the
    one subject this surface exists to be believed about.

    The one notification this feature sends, and the reason it can be sent at
    all: a plan comes due minutes or days after the session that made it, from a
    merge this machine may have had nothing to do with, so there is frequently
    nobody looking at the UI when it happens. Guarded on ``cfg.active`` (the same
    check every other push path makes) and wrapped, because a notification must
    never be able to stop the loop that produced it.
    """
    try:
        if plan.get("notified_at"):
            return
        cfg = _ntfy.load()
        # Stamped whether or not ntfy is on, and before the send rather than
        # after it: the stamp means "this plan's shipping moment has passed", not
        # "a push succeeded". Switching notifications ON next week must not
        # produce a backlog of announcements about work that shipped last month.
        _test_plans.mark_notified(str(plan.get("id") or ""))
        if not cfg.active:
            return
        # The ``verify_plan_due`` rule's switch governs this push as well as
        # the browser notification (the rule itself is ``ntfy: False`` so the
        # generic dispatch doesn't send it twice).
        from backend.web.addons import notify as _notify

        if not _notify.rule_enabled("verify_plan_due"):
            return
        steps = plan.get("steps") or []
        mine = sum(1 for s in steps if s.get("actor") == "human")
        _ntfy.publish_soon(
            cfg,
            title="%s shipped to %s"
            % (
                plan.get("title") or plan.get("id"),
                plan.get("live_branch") or "the live branch",
            ),
            # Two numbers, because they are not the same number and the old
            # message implied they were: "4 step(s) to check on main" reads as
            # four things for the reader to do, when three of them are the
            # agent's. It also garden-paths — "check on main" parses as "look in
            # on main" before it parses as "check, on main" — which is why the
            # branch has moved up into the title, where it is a fact about what
            # happened rather than a place to go.
            message="%d step%s in its checklist, %d for you."
            % (len(steps), "" if len(steps) == 1 else "s", mine),
            # 3 = normal. This is news, not an emergency: nothing is broken and
            # nothing is waiting on the user, unlike the rules that ring at 4.
            priority=3,
            tags=["white_check_mark"],
        )
    except Exception as err:  # noqa: BLE001
        _ntfy.log_error("test plan due push failed: %s", err)


def _test_plan_run_started_at(plan: dict) -> float:
    """When the plan's CURRENT run began (0.0 when there isn't one).

    Read off the run record ``start_run`` opened rather than tracked in memory,
    so a server restart mid-run does not reset the give-up clock — a wedged
    verify session must not become immortal by outliving the process watching it.
    """
    for run in reversed(plan.get("runs") or []):
        if run.get("session") == plan.get("run_session"):
            return float(run.get("at") or 0.0)
    return 0.0


def _read_verify_results(plan: dict) -> Optional[dict]:
    """The run session's ``.mindflock_verify.json``, or ``None``.

    This file IS the verify session's return channel: the run is an ordinary
    session in a real workspace with no exit code and no callback (exactly the
    problem ``live_stage`` exists for), so the agent is told to write its answers
    to one git-excluded file and the due loop reads it. Absent, half-written or
    malformed all read the same way — "not finished yet" — because the poller
    runs every 60s and the next pass is a free retry.
    """
    inst = _server().ENGINE.instances.get(plan.get("run_session") or "")
    if inst is None:
        return None  # session deleted; test_plans.prune releases the plan
    try:
        wt = inst.GetWorktreePath()
    except Exception:  # noqa: BLE001
        wt = ""
    if not wt:
        return None
    try:
        with open(
            os.path.join(wt, _test_plans.RESULT_FILE), "r", encoding="utf-8"
        ) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # IT MUST BE ABOUT THIS PLAN. The prompt asks the agent to echo the plan id
    # and nothing checked it, so a result file left behind by an earlier plan in
    # a reused worktree — or one an agent wrote from an example — could settle a
    # checklist it never read. Blank is tolerated: an older build's file has no
    # id, and refusing those would strand every run in flight across an upgrade.
    claimed = str(data.get("plan") or "")
    if claimed and claimed != str(plan.get("id") or ""):
        return None
    return data


def _free_stale_verify_worktree(repo_root: str, title: str) -> str:
    """Release a worktree a DEAD verify session left holding this run's branch.

    THE TRAP THIS ENDS, and it is a permanent one rather than a flaky one. A
    verify session is named for its plan and its commit (``verify-<plan>-<sha7>``)
    precisely so that "same commit" and "different commit" are different
    sessions — which also makes the branch it wants
    (``<branch_prefix>verify-<plan>-<sha7>``) the SAME name every time. Git will
    not check a branch out in two worktrees at once, so the moment one verify
    worktree is left behind — the app killed mid-run, a session removed from the
    engine without its worktree being reclaimed, a crash — every subsequent run
    of that checklist dies in ``Start`` with::

        fatal: '<branch>' is already used by worktree at '<path>'

    and the plan silently reverts. Not once: forever, for that checklist, with no
    control anywhere in the product that clears it.

    ONLY WHAT NOTHING OWNS. The live sessions' worktree paths are collected
    first and never touched — this must not be able to reclaim the tree an agent
    is working in, which is the one thing worse than the bug it fixes. A
    worktree registered to a path that no longer exists on disk is also fair
    game (that is what ``git worktree prune`` is for) and is the commonest shape
    of the leftover.

    Returns a short description of what it freed, for the log; "" when there was
    nothing to do, which is the normal case.
    """
    repo = str(repo_root or "")
    if not repo or not _server().git_available():
        return ""
    try:
        from backend.config import config as _config
        from backend.session.git.worktree import sanitize_branch_name

        want = sanitize_branch_name(
            "{}{}".format(_config.LoadConfig().branch_prefix, title)
        )
    except Exception:  # noqa: BLE001 — a cleanup that cannot name its target
        return ""  # is a cleanup that must not run

    live: set = set()
    for inst in list(_server().ENGINE.instances.values()):
        try:
            path = inst.GetWorktreePath()
        except Exception:  # noqa: BLE001
            path = ""
        if path:
            live.add(os.path.abspath(path))

    worktrees = gitutil.worktree_list(repo, timeout=30)
    if worktrees is None:
        return ""

    def _remove(path: str, branch: str) -> None:
        # Best effort, in order (a failed step never raises).
        for args in (
            ["worktree", "remove", "--force", path],
            ["worktree", "prune"],
        ) + ((["branch", "-D", branch],) if branch else ()):
            gitutil.git(
                args,
                repo,
                stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )

    # The directory name a worktree for THIS session would have:
    # ``<worktrees dir>/<sanitized branch>_<hex>``, so the basename is the last
    # segment of the branch plus an underscore. Matched by name because the
    # branch line cannot be relied on — see the detached case below.
    leaf = want.rsplit("/", 1)[-1] + "_"

    freed = ""
    for wt in worktrees:
        path = str(wt["path"])
        detached = bool(wt.get("detached"))
        branch = str(wt.get("branch") or "").split("refs/heads/", 1)[-1]
        if not path:
            continue
        # A LEFTOVER IS NOT ALWAYS ON ITS BRANCH, and that is the case this
        # feature actually produces: a verify run's first act is to check out the
        # commit it is verifying, so the worktree ends up DETACHED and the branch
        # is often gone entirely. Matching only the branch line therefore missed
        # every leftover from a run that had actually run — they accumulate in
        # `~/.mindflock/worktrees`, a full checkout each, and nothing in the
        # product ever removes them. So a directory named for this session
        # counts too, and it is just as safe: the name carries the plan AND the
        # sha, and a live session's path is never touched.
        mine = branch == want or (
            (detached or not branch) and os.path.basename(path).startswith(leaf)
        )
        if not mine:
            continue
        if os.path.abspath(path) in live:
            # An agent is in there right now. Whatever is wrong, this is not
            # ours to take.
            return ""
        _remove(path, branch if branch == want else "")
        freed = ("%s (%s)" % (branch, path)) if branch else path
    return freed


def _kill_orphan_plan_tmux(title: str) -> str:
    """Kill the tmux sessions a DEAD run of this checklist left behind.

    THE OTHER HALF OF :func:`_free_stale_verify_worktree`, and the same trap
    from the other end. A verify session is named for its plan and its commit,
    so the tmux session it wants (``mindflock_verify-<plan>-<sha7>``) has the
    SAME name every time that checklist is run for that commit. tmux outlives
    this process: kill the app mid-run, delete the session while its window is
    detached, lose the engine's record in a crash, and the tmux session survives
    with nothing owning it. Every later run of that checklist then dies in
    ``Instance.Start`` with

        failed to start new session: tmux session already exists: mindflock_verify-…

    which is exactly what the owner saw. It dies LATE, too — on the background
    start task, after the workspace was provisioned and after the route already
    answered 202 — so the plan is stamped ``running``, then reverts with that
    sentence recorded on it and no control anywhere in the product to clear it.
    Permanent for that checklist, like the worktree case, and invisible: nothing
    in the session list shows an orphan, because nothing in the app owns it.

    ONLY WHAT NOTHING OWNS. Called for a ``verify-`` or ``fix-`` title with no
    live instance, it kills exactly the two sessions named for that title (the
    agent window and its shell) and nothing else. Both are sessions THIS FEATURE
    creates, from a name it derives, for work it is about to start again — which
    is what makes killing them safe in a way it would not be for a session
    somebody named themselves. Note the asymmetry with the worktree: a window is
    a process, and killing an unowned one loses nothing that was not already
    lost, while a fix session's TREE can hold uncommitted work and is only ever
    reclaimed when it is provably pristine (:func:`_reclaim_plan_worktree`).

    Returns what it killed, for the log; "" when there was nothing, which is the
    normal case. Never raises.
    """
    srv = _server()
    title = str(title or "")
    # Belt and braces: the caller checks both of these, and this must never be
    # reachable for a session a person owns.
    if not title.startswith(("verify-", "fix-")) or title in srv.ENGINE.instances:
        return ""
    killed = []
    try:
        names = [tmux.to_mindflock_tmux_name(title), srv._shell_tmux_name(title)]
    except Exception:  # noqa: BLE001 — a cleanup that cannot name its target
        return ""  # is a cleanup that must not run
    for name in names:
        try:
            if srv._live_session_name(name) is None:
                continue
            rc = srv._run_capped(
                ["tmux", "kill-session", "-t=" + name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            ).returncode
            if rc == 0:
                killed.append(name)
        except Exception:  # noqa: BLE001 — best effort, one name at a time
            continue
    return ", ".join(killed)


def _reclaim_plan_worktree(repo_root: str, title: str) -> Tuple[str, str]:
    """Free the branch a dead ``fix-`` session is holding — ONLY if it is safe.

    The same permanent wedge as :func:`_free_stale_verify_worktree`, on a
    session with the opposite contract. A fix session's whole job is to CHANGE
    the tree, so its worktree can hold work nobody has committed, and the verify
    reclaim's ``worktree remove --force`` + ``branch -D`` would delete exactly
    that. ``worktree_reclaim.reclaim_for_branch`` refuses anything that is not
    pristine and anything a live session owns, so the wedge clears itself in the
    (common) case where the leftover is empty and stays put in the case where
    removing it would lose work — where the route's own error is then the honest
    answer.

    Returns ``(reclaimed, held)``: the path it freed, or the path it REFUSED to
    free and why the caller must stop. Both empty is the normal case — nothing
    was holding the branch. Never raises.

    THE THIRD ANSWER IS THE POINT. A decline used to look exactly like "nothing
    to do", so the route created the session anyway and the collision surfaced
    minutes later inside ``_bg_start`` as a raw git line in the notifications
    bell — nowhere near the checklist, and with no hint that the fix for it is a
    directory full of somebody's uncommitted work. The route can now say that
    synchronously.
    """
    repo = str(repo_root or "")
    if not repo or not _server().git_available():
        return "", ""
    try:
        from backend.config import config as _config
        from backend.session.git.worktree import sanitize_branch_name
        from backend.session.provisioned import worktree_holding_branch
        from backend.web.core import worktree_reclaim as _reclaim

        branch = sanitize_branch_name(
            "{}{}".format(_config.LoadConfig().branch_prefix, title)
        )
        held_at = worktree_holding_branch(repo, branch) or ""
        if not held_at:
            return "", ""
        live = set()
        for inst in list(_server().ENGINE.instances.values()):
            try:
                path = inst.GetWorktreePath()
            except Exception:  # noqa: BLE001
                path = ""
            if path:
                live.add(os.path.abspath(path))
        freed = _reclaim.reclaim_for_branch(
            repo, branch, lambda path: os.path.abspath(path) in live
        )
        return (freed, "") if freed else ("", held_at)
    except Exception:  # noqa: BLE001 — a cleanup that cannot name its target
        return "", ""  # is a cleanup that must not run


def _verify_session_usable(title: str) -> bool:
    """Whether the open verify session ``title`` can actually be sent work.

    A session record outlives its workspace: the worktree can be reclaimed,
    removed by hand, or lost with the disk it was on, and the engine goes on
    holding the instance. Everything that matters downstream — sending a prompt,
    reading the result file — needs the directory, so "is there a record" is the
    wrong question and was the one being asked.
    """
    inst = _server().ENGINE.instances.get(title)
    if inst is None:
        return False
    try:
        wt = inst.GetWorktreePath()
    except Exception:  # noqa: BLE001
        return False
    return bool(wt) and os.path.isdir(wt)


def _test_plan_row(plan: dict, live_branch: Optional[str] = None) -> dict:
    """One plan, shaped the way every reader of this feature expects it.

    THE TWO EDITS THE STORE DOES NOT DO, in one place because a second reader
    now exists (``POST /run`` answers with the plan it just started, and the
    client REPLACES its whole row with it):

    * ``effective_live_branch`` — what THIS PLAN'S REPO calls live, which is the
      only branch its own stamp can honestly be compared with. Without it a row
      falls back to the flock-wide default and a repo that ships from ``staging``
      starts reading as "written against a branch that has since moved".
    * ``conversation`` — the snapshotted transcript is generation input, never
      UI. Nothing renders it, and it is up to CONV_BUDGET of somebody's session
      text per plan.

    Mutates and returns the same dict, which is what the list route wants.
    """
    root = str(plan.get("repo_root") or "")
    plan["effective_live_branch"] = (
        live_branch
        if live_branch is not None
        else _test_plans.resolve_live_branch(root)
    )
    plan.pop("conversation", None)
    return plan


def _is_verify_repo_usable(repo_root: str) -> bool:
    """Whether a run can actually be started in ``repo_root``.

    A plan records the MAIN repo rather than the worktree, precisely so it
    outlives the session that produced it — and over the weeks a checklist can
    wait, that path can be moved, renamed or deleted. Nothing downstream
    notices: ``_prepare_plain_repo`` creates a missing path rather than
    refusing, so the run gets a real session in an empty non-git folder.

    A directory that exists but is not a git repository counts as unusable for
    the same reason: the run's first instruction is to check out the live
    branch. Never raises — an unreadable path is not a usable one.
    """
    root = str(repo_root or "")
    if not root:
        return False
    try:
        if not os.path.isdir(root):
            return False
        return (
            _server()
            ._run_capped(
                ["git", "-C", root, "rev-parse", "--git-dir"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=30,
            )
            .returncode
            == 0
        )
    except Exception:  # noqa: BLE001
        return False


def _clear_verify_results(session_title: str) -> None:
    """Delete a verify session's result file, if it has one. Never raises.

    Keyed by session title rather than by plan, because it runs before the plan
    knows which session it is about to get. A session that does not exist yet
    (the first run) has nothing to clear, which is why every failure here is a
    no-op rather than an error.
    """
    inst = _server().ENGINE.instances.get(session_title)
    if inst is None:
        return
    try:
        wt = inst.GetWorktreePath()
        if wt:
            os.unlink(os.path.join(wt, _test_plans.RESULT_FILE))
    except Exception:  # noqa: BLE001 — no file, no worktree, no permission
        pass


def _announce_test_plan_checked(plan: dict) -> None:
    """Say that a run finished, and whether it left anything.

    Starting a run is loud — a toast, a row that changes, a terminal that opens
    — and finishing one was completely silent: the results landed in a dialog
    the user had almost certainly navigated away from, minutes later. The event
    carries the two numbers that decide whether anything is being asked of them,
    so the client can say "8 passed, 3 need your eyes" without a second fetch.
    """
    try:
        pid = str(plan.get("id") or "")
        run = (plan.get("runs") or [{}])[-1]
        results = run.get("results") or {}
        failed = sum(1 for r in results.values() if r.get("result") == "fail")
        # THE TWO NUMBERS MUST NOT COUNT THE SAME STEP. Written as "not a pass
        # and not answered by a person", this counted every agent-recorded FAIL
        # in both — so a run finding one broken step announced "1 step failed, 1
        # step needs you", which reads as two problems and is one.
        #
        # Mirrors `stepCheck`'s "yours" in verify.ts, which is what the row will
        # say when the user opens the dialog the toast sent them to: a step is
        # yours when nobody has settled it AND either it was always yours (a
        # human actor) or the agent explicitly handed it back (`blocked`).
        # `pass` and `fail` are settled; a `blocked` a PERSON recorded is their
        # "can't check", which is an answer.
        needs_you = 0
        for step in plan.get("steps") or []:
            entry = results.get(step.get("id")) or {}
            result = entry.get("result") or ""
            if result in ("pass", "fail"):
                continue
            if result == "blocked" and entry.get("by") == "human":
                continue
            if step.get("actor") == "human" or result == "blocked":
                needs_you += 1
        _events.BUS.emit(
            "session.test_plan_checked",
            session=pid,
            # "failed" when any step failed, "" otherwise: what the
            # default-on ``verify_run_failed`` notify rule matches on (a rule
            # can only look at ``old``/``new``, never inside ``data``).
            new="failed" if failed else "",
            data={
                "plan": pid,
                "title": plan.get("title") or pid,
                "failed": failed,
                "needs_you": needs_you,
                # A check of the BRANCH, before merge: the plan stays open and
                # comes back after merge, so a toast must not read as "done".
                "before_merge": bool(run.get("prelive")),
            },
        )
    except Exception:  # noqa: BLE001 — an announcement must never stop the pass
        pass


def _verify_run_trees(plan: dict) -> tuple:
    """``(tested_sha, expected_sha)`` for a run that has just answered.

    THE CLAIM THIS MAKES CHECKABLE. ``build_run_prompt`` spends a paragraph on
    why a verify run must not be able to test the wrong tree, and until this the
    whole mechanism was a sentence asking the agent to check out
    ``origin/<live>``. A fetch that failed quietly left the agent working
    whatever HEAD the worktree was cut from — typically the clone's LOCAL live
    branch, which is behind origin — and the plan then recorded "it works" about
    a tree nobody could name.

    Both answers are best-effort and both may be blank, which
    ``test_plans.run_tree_mismatch`` reads as "unknown", never as "mismatched":
    the cost of a wrong guess here is throwing away a good run's answers.

    A plan run BEFORE it shipped is compared against its own commit, which is
    what that arm of the run prompt asked for and the tree those steps are about.
    """
    inst = _server().ENGINE.instances.get(plan.get("run_session") or "")
    if inst is None:
        return "", ""
    try:
        wt = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        wt = ""
    if not wt:
        return "", ""
    tested = _server()._git_head_sha(wt)
    live = str(plan.get("live_branch") or "").strip()
    if not plan.get("live_at"):
        # "Check it early" — the honest expectation is the branch's own tip.
        return tested, str(plan.get("tip_sha") or plan.get("sha") or "")
    root = str(plan.get("repo_root") or "") or wt
    expected = ""
    for ref in ("origin/%s" % live, live):
        if not live:
            break
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "--verify", "-q", ref],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
        if out.returncode == 0:
            expected = out.stdout.decode("utf-8", "replace").strip()
            break
    return tested, expected


#: When each running verify session was first seen with no tmux window, and how
#: long that has to hold before the run is released. One miss is not proof: tmux
#: can be briefly unreachable, and `create_instance` registers the record a
#: moment before `Start` makes the window.
_VERIFY_DEAD_SINCE: Dict[str, float] = {}
_VERIFY_DEAD_GRACE_S = 120.0


def _verify_window_gone(run_session: str, now: Optional[float] = None) -> bool:
    """Whether ``session``'s agent window has been missing long enough to act on.

    Cheap in the normal case — one ``tmux has-session`` per running plan, and
    there is usually at most one. Never raises: a probe that cannot answer is
    not evidence of death, so it forgets the session and starts the clock again.

    A SESSION THAT HAS NOT COME UP YET IS NOT A DEAD ONE, and this is the trap
    worth naming: `start_run` stamps the plan the moment the route answers 202,
    while the workspace behind it is still being made — a cold base clone plus a
    dependency install runs for MINUTES, with no tmux window for any of it. A
    bare "no window" test would therefore give up on every first run in a new
    repo, halfway through provisioning it. A paused session is the same
    argument: its window is deliberately gone and its run is not. Both are left
    to the clock they already have (the two-hour deadline) and to `prune`, which
    owns the case where the record itself has disappeared.
    """
    ts = float(now if now is not None else time.time())
    inst = _server().ENGINE.instances.get(run_session)
    if inst is None:
        _VERIFY_DEAD_SINCE.pop(run_session, None)
        return False
    try:
        # `Paused` by name rather than `session.Paused`: an earlier draft of
        # this took the title in a parameter called `session`, which shadowed the
        # `session` MODULE — the attribute form then resolved against a string,
        # raised, and the except below read that as "not evidence", so the check
        # could never fire at all. The parameter is `run_session` now; the plain
        # name stays because it cannot be shadowed by accident.
        if not inst.Started() or inst.Status in (Loading, Paused):
            _VERIFY_DEAD_SINCE.pop(run_session, None)
            return False
    except Exception:  # noqa: BLE001 — an unreadable status is not evidence
        _VERIFY_DEAD_SINCE.pop(run_session, None)
        return False
    # THREE ANSWERS, NOT TWO. `tmux has-session` exits 0 for a live session and
    # 1 for a missing one; anything else — a tmux server that is not answering,
    # the 124 `_run_capped` returns when it kills a hung probe — means the
    # question was not answered at all. Folding that into "missing" is how a
    # loaded machine gives up on a run that is working: two minutes of slow
    # probes and the plan is released out from under a live agent.
    try:
        rc = (
            _server()
            ._run_capped(
                [
                    "tmux",
                    "has-session",
                    "-t=" + tmux.to_mindflock_tmux_name(run_session),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            .returncode
        )
    except Exception:  # noqa: BLE001
        rc = -1
    if rc != 1:
        _VERIFY_DEAD_SINCE.pop(run_session, None)
        return False
    first = _VERIFY_DEAD_SINCE.setdefault(run_session, ts)
    return ts - first >= _VERIFY_DEAD_GRACE_S


def _release_wedged_run(
    pid: str, run_session: str, reason: str, hours: int = 0
) -> None:
    """Put a plan back and SAY WHY, for a run that is never going to report.

    Both callers — the two-hour deadline and the dead window — need the same
    three things to happen together, and the one that mattered most was missing
    from both: the sentence. `mark_due` alone put the row back to "nobody has
    checked it yet", so a session that had been started, been billed and died
    read exactly like a button nobody pressed.
    """
    released = _test_plans.mark_due(pid, reason=reason)
    if released is None:
        return
    _VERIFY_DEAD_SINCE.pop(run_session, None)
    # `mark_due` cleared `run_session` AND dropped the empty run record, which is
    # what actually hands the session to `_sweep_orphan_verify_sessions`: it
    # keeps any session a run record still names, so before that pop the
    # abandoned agent was exempt from the one thing that closes strays.
    _events.BUS.emit(
        "session.test_plan_gave_up",
        session=pid,
        data={
            "plan": pid,
            "title": released.get("title") or pid,
            "run_session": run_session,
            # WHICH release this was. Two things end a run without a report — a
            # window that died in the first minutes and a deadline two hours
            # later — and an event carrying only `hours` (0 for one of them)
            # made them indistinguishable to anyone reading the bus, while the
            # sentence the plan got said exactly which. It travels with it.
            "hours": hours,
            "reason": reason,
        },
    )


def _poll_running_test_plans(plans: list) -> None:
    """Fold in the results of every verify session that has finished.

    Deliberately its own phase, run BEFORE the liveness checks and never behind
    them: this is the half of the pass that is purely local (one ``open()`` per
    running plan) and the half a person is actually waiting on — the verify
    session on their screen has written its answers and the plan still says
    "running". Sharing one loop with the network half meant a plan waiting to go
    live could hold the results of a finished run hostage for as long as origin
    took to answer, which is also how the two-hour give-up clock stopped being
    evaluated. Ordering costs nothing and removes the coupling entirely.
    """
    live_titles = set()
    for plan in plans:
        if plan["state"] != "running":
            continue
        pid = plan["id"]
        # NOT `session`: that is an imported MODULE name, and shadowing it in a
        # function that also asks about instance status is how a guard
        # silently stops guarding (see `_verify_window_gone`).
        run_session = str(plan.get("run_session") or "")
        live_titles.add(run_session)
        try:
            data = _read_verify_results(plan)
            if data and data.get("finished"):
                results = data.get("results")
                tested, expected = _verify_run_trees(plan)
                done = _test_plans.finish_run(
                    pid,
                    results if isinstance(results, list) else [],
                    tested_sha=tested,
                    expected_sha=expected,
                    target=_test_plans.verify_target(plan.get("repo_root") or ""),
                )
                # Only when the store actually took it. `finish_run` answers
                # None for a plan that was deleted while the run was working,
                # and announcing that one would toast about a checklist that is
                # no longer there.
                if done:
                    # THE ANSWER IS EATEN, not left on disk. This file is the
                    # session's whole return channel and the poller believes the
                    # first `finished: true` it sees, so a copy left behind is a
                    # loaded gun: the next run of the same plan is finished by
                    # its predecessor within 60s — before the agent has checked
                    # anything out — and takes the old verdict as the new one.
                    # `/run` clears it too, but only while an engine record still
                    # points at the worktree, which is exactly what a sweep, a
                    # cancel or a restart takes away.
                    _clear_verify_results(run_session)
                    _announce_test_plan_checked(done)
                continue
            started = _test_plan_run_started_at(plan)
            # THE WINDOW IS GONE — the commonest way a run dies, and the one the
            # two-hour clock served worst. An agent that hits a usage limit, is
            # killed in its pane, or whose tmux server went down leaves the
            # engine record intact, so nothing here noticed: the row said "an
            # agent is checking the steps it can" and offered Watch onto a dead
            # pane for two hours. A single miss is not proof (tmux can be
            # briefly unreachable, and a session is created a moment before it
            # is recorded), so the miss has to persist.
            if run_session and _verify_window_gone(run_session):
                _release_wedged_run(
                    pid,
                    run_session,
                    # SHORT FIRST SENTENCE, deliberately: the collapsed row
                    # lifts sentence one and nothing else (`errorHeadline`), so
                    # a paragraph-long opener is a release nobody can see.
                    "The verify session's agent window is gone. Nothing was "
                    "running any more, and nothing it may have found was "
                    "recorded — run it again.",
                )
                continue
            if started and time.time() - started > _TEST_PLAN_RUN_GIVE_UP_S:
                # NOT SILENTLY. This is a real session that was started, was
                # billed for two hours and never wrote an answer; releasing the
                # plan without a word turned that into "nobody has checked it
                # yet", which reads as the button never having been pressed.
                # The sentence lands on the plan (the row says it) and on the
                # bus (so it reaches someone who is not looking at the dialog —
                # which, two hours in, is everyone).
                hours = int(_TEST_PLAN_RUN_GIVE_UP_S // 3600) or 1
                _release_wedged_run(
                    pid,
                    run_session,
                    "The verify run was given up on after %d hours. %s never "
                    "wrote its answers, so nothing it may have found was "
                    "recorded — run it again." % (hours, run_session or "The session"),
                    hours=hours,
                )
        except Exception:  # noqa: BLE001 — one bad plan can't stop the pass
            pass
    # A cursor, not a cache: anything not still running this pass is forgotten,
    # so the map cannot outgrow the runs it is watching.
    for key in [k for k in _VERIFY_DEAD_SINCE if k not in live_titles]:
        _VERIFY_DEAD_SINCE.pop(key, None)


def _liveness_order(plans: list) -> list:
    """The ``generated`` plans, least-recently-asked first.

    The rotation that makes :data:`_TEST_PLAN_LIVE_BUDGET_S` fair. A pass that
    simply stopped at the budget would re-ask the same head of a fixed list
    every minute and never reach the tail, so the plans at the back would wait
    for their work to go live forever. Sorting by when each plan was last asked
    turns the budget into a round-robin: whatever got skipped last time is at
    the front this time. Never-asked plans (0.0) go first, and within a tie the
    store's own newest-first order stands.
    """
    candidates = [p for p in plans if p["state"] == "generated"]
    ids = {p["id"] for p in candidates}
    # Forget plans that have left ``generated`` (marked due, deleted, pruned) so
    # this map cannot outgrow the store it is a cursor into.
    for key in [k for k in _TEST_PLAN_LIVE_CHECKED if k not in ids]:
        _TEST_PLAN_LIVE_CHECKED.pop(key, None)
    return sorted(candidates, key=lambda p: _TEST_PLAN_LIVE_CHECKED.get(p["id"], 0.0))


def _check_test_plans_for_liveness(plans: list) -> None:
    """Ask origin whether any waiting plan's work has shipped, within a budget.

    The expensive phase: each plan costs a ``git fetch`` (up to
    ``test_plans.TIMEOUT_FETCH``, 120s) and sometimes a ``gh`` call on top, and
    those caps are per CALL — a dozen plans waiting on a remote that blackholes
    TCP add up to a pass measured in tens of minutes, which is the whole loop
    gone, not just this half of it. So the phase gets a wall-clock budget and
    stops when it runs out; :func:`_liveness_order` makes sure the plans it did
    not reach are the ones it starts with next minute.

    The "last asked" stamp is written BEFORE the check, not after: a plan whose
    repo hangs for the full timeout has been asked, expensively, and must go to
    the back of the queue rather than monopolize the head of it.
    """
    # Nothing can reach a live branch without git; skip the fetch storm entirely
    # on a machine that hasn't got it.
    if not _server().git_available():
        return
    # Paused. "Off" has to mean the feature is quiet, not merely that no NEW
    # plans get written: a paused Verify that still fetched every minute and
    # kept moving plans into `due` would carry on lighting the top-bar badge
    # with work the user has just said they do not want chased. Nothing is lost
    # — the plans keep their state and the next enabled pass picks them up.
    if not _verify_enabled():
        return
    # A DUE plan follows the setting too — when nobody has answered anything.
    # `_liveness_order` never visits `due` (there is nothing to ask origin
    # about), so without this pass a checklist that went due against the OLD
    # branch — the observed case went due on `main` for a PR that had merged
    # into `staging` — sat in the badge telling the user to "change the live
    # branch on this repo's card" while the changed card changed nothing.
    # `retarget_live_branch` owns the gate (a plan with a settled answer keeps
    # its branch) and puts the mover back to waiting, where the rotation below
    # picks it up on the next pass. Costs a settings read per due plan, no git.
    for plan in plans:
        if plan.get("state") != "due":
            continue
        try:
            _test_plans.retarget_live_branch(
                plan["id"],
                _test_plans.resolve_live_branch(plan.get("repo_root") or ""),
            )
        except Exception:  # noqa: BLE001 — one unreadable plan must not stall the pass
            pass
    deadline = time.monotonic() + _TEST_PLAN_LIVE_BUDGET_S
    for plan in _liveness_order(plans):
        if time.monotonic() >= deadline:
            break
        pid = plan["id"]
        try:
            # WHICH BRANCH ARE WE EVEN WATCHING? Asked every pass, because the
            # answer is a live setting: a repo re-pointed from `staging` to
            # `main` must re-aim the checklists that have not gone due yet, or
            # the setting is a lie for exactly the plans that exist right now.
            # `retarget_live_branch` owns the gate (and clears any merge stamp,
            # which was recorded against the branch we just stopped watching).
            moved = _test_plans.retarget_live_branch(
                pid, _test_plans.resolve_live_branch(plan.get("repo_root") or "")
            )
            if moved is not None:
                plan = moved
            # MERGED AND DEPLOYED ARE TWO FACTS. Ancestry is true the instant a
            # PR lands; what a checklist tests is a running service the pipeline
            # reaches minutes later, and a plan marked due in that window gets
            # answered against the behaviour the change replaces — a FAIL
            # recorded on correct code, which is the one outcome this surface
            # cannot survive. So the merge starts a clock and the clock is what
            # marks it due.
            if not plan.get("merged_at"):
                # Not seen merged yet: this is the expensive question (a fetch,
                # sometimes a `gh` call), so it is the only branch that is
                # rate-limited by the rotation stamp below.
                _TEST_PLAN_LIVE_CHECKED[pid] = time.time()
                if not _test_plan_is_live(plan):
                    continue
                if _test_plans.mark_merged(pid) is None:
                    continue  # deleted while we were asking origin
                plan = _test_plans.get(pid) or plan
            # ...and from here on there is nothing left to ask origin. A plan
            # waiting out its deploy window costs one clock comparison per pass
            # instead of a fetch, so adding the wait made this loop cheaper.
            delay = _test_plans.resolve_deploy_delay(plan.get("repo_root") or "")
            if not _test_plans.deploy_ready(plan, delay):
                continue
            due = _test_plans.mark_due(pid)
            if due is None:
                continue  # deleted while it was waiting
            _events.BUS.emit(
                "session.test_plan_due",
                session=pid,
                data={
                    "plan": pid,
                    "title": due["title"],
                    "live_branch": due["live_branch"],
                },
            )
            _notify_test_plan_due(due)
        except Exception:  # noqa: BLE001 — one bad plan can't stop the pass
            pass


def _check_test_plan_landings(plans: list) -> None:
    """Refresh "which branch has this reached on origin" for the cards that show it.

    A SEPARATE PASS FROM LIVENESS, over a different set of plans, on purpose.
    The liveness rotation visits ``generated`` plans only — everything else has
    nothing left to ask origin *about the branch it ships from*. But every card
    on the surface shows where its work landed, including the ones that are due,
    running or answered, and a checklist written before this existed has no
    answer at all. So this walks all of them.

    What stops it being expensive:

    * **A landing that has stopped moving is finished being asked** —
      :data:`_TEST_PLAN_LANDED_SETTLE_S` after the work last landed somewhere,
      wherever that was. See the comment on the check itself for why this is a
      clock and not "it reached the branch this repo ships from", which is what
      it used to be and which froze the card one rung short in every repo that
      promotes a live branch onward.
    * **:data:`_TEST_PLAN_LANDED_TTL_S`** — minutes, not every tick. Nothing acts
      on this answer, so nothing is hurt by it being a few minutes old.
    * **The fetch is per repository, not per plan** (``fetch_all_heads``), so a
      dozen checklists in one repo cost one fetch between them.
    * **A wall-clock budget**, least-recently-asked first, exactly like the
      liveness pass — and for the same reason: the per-call caps are per CALL,
      and a remote that blackholes TCP would otherwise spend the whole loop here.
    """
    if not _server().git_available() or not _verify_enabled():
        return
    live = {p["id"] for p in plans}
    for key in [k for k in _TEST_PLAN_LANDED_CHECKED if k not in live]:
        _TEST_PLAN_LANDED_CHECKED.pop(key, None)
    now = time.time()
    todo = []
    for plan in plans:
        if not (plan.get("repo_root") and plan.get("branch") and plan.get("sha")):
            continue
        # WHEN THIS QUESTION IS FINISHED — and it is NOT "the work reached the
        # branch this repo ships from", which is what stood here first and what
        # froze the card in exactly the repo shape the answer exists for. A repo
        # that merges PRs into `staging` and promotes `staging` into `main` hours
        # later has `staging` as BOTH its first rung and its live branch, so
        # every plan stopped being asked at the first rung and a screenful of
        # cards went on saying "staging" about work that was already in `main`.
        # The climb the row renders (local -> staging -> main) does not end at
        # the live branch; it ends when the work stops moving.
        #
        # So the stop is a settle window on the landing itself, which also
        # bounds the queue for EVERY landed plan instead of only the ones that
        # happened to end on the live branch — the develop/release repo the old
        # rule re-probed for ever included.
        landed = str(plan.get("merged_into") or "")
        # `merged_into_at` is 0.0 for a landing only the PR could name (a squash
        # merge leaves no ancestry, and a PR does not carry the moment it
        # merged), so fall back through the stamps that do exist rather than
        # asking such a plan for ever. `generated_at` is the last resort and is
        # always set, so the window always closes.
        since = (
            float(plan.get("merged_into_at") or 0.0)
            or float(plan.get("merged_at") or 0.0)
            or float(plan.get("live_at") or 0.0)
            or float(plan.get("generated_at") or 0.0)
        )
        if landed and since and now - since > _TEST_PLAN_LANDED_SETTLE_S:
            continue
        if (
            now - _TEST_PLAN_LANDED_CHECKED.get(plan["id"], 0.0)
            < _TEST_PLAN_LANDED_TTL_S
        ):
            continue
        todo.append(plan)
    todo.sort(key=lambda p: _TEST_PLAN_LANDED_CHECKED.get(p["id"], 0.0))
    deadline = time.monotonic() + _TEST_PLAN_LANDED_BUDGET_S
    for plan in todo:
        if time.monotonic() >= deadline:
            break
        # Stamped BEFORE the ask, like the liveness cursor: a plan whose repo
        # hangs for the full timeout has been asked, expensively, and must go to
        # the back of the queue rather than monopolize the head of it.
        _TEST_PLAN_LANDED_CHECKED[plan["id"]] = time.time()
        try:
            found = _test_plan_merged_into(plan)
            _test_plans.set_merged_into(
                plan["id"],
                found.get("branch") or "",
                float(found.get("at") or 0.0),
                list(found.get("all") or []),
            )
        except Exception:  # noqa: BLE001 — one bad repo can't stop the pass
            pass


def _test_plans_due_pass(full: bool = True) -> bool:
    """One pass of the due loop. Blocking; runs via ``to_thread``.

    Two jobs on one cadence, deliberately not two loops: both are "look at every
    plan and see whether the world moved", and a second 60s timer would only
    double the wakeups. They are two PHASES rather than one interleaved loop
    because only one of them talks to the network — see
    :func:`_poll_running_test_plans` for what that coupling cost.

    ``full`` is what lets the two phases keep one loop while running at two
    speeds. A pass with ``full=False`` does the local half ONLY: it reads the
    results file of anything that is running, and touches neither the network nor
    the store's housekeeping. That is the pass the loop runs every few seconds
    while a verify session is in flight, so an agent that has just finished on
    the user's screen is reflected almost immediately instead of up to a minute
    later. The expensive half — prune, stalled-generation recovery, and the
    ``git fetch`` per waiting plan — stays strictly on the minute.

    Returns whether anything is still running, which is how the loop picks its
    next sleep without a second store read.

    Every plan is wrapped on its own. A repo that was deleted, a remote that
    hangs, a run session in a broken state — none of them may stop the pass, or
    one bad plan silently switches the feature off for every other one.
    """
    if full:
        # Release plans whose verify session no longer exists (the user deleted
        # it mid-run, so nothing is left to write the results file) and enforce
        # the store's cap. Explicitly NOT a liveness prune of the plans
        # themselves — plans are supposed to outlive their sessions; see
        # test_plans.prune.
        try:
            _test_plans.prune(list(_server().ENGINE.instances.keys()))
        except Exception:  # noqa: BLE001
            pass
    plans = _test_plans.list_plans()
    # Cheap and local (no network, no model call on this thread), so it runs
    # before the phase that can spend the whole pass talking to origin: a plan
    # abandoned mid-generation must not have to wait behind a fetch storm to be
    # picked back up.
    if full:
        _recover_stalled_test_plans(plans)
        _retry_no_commit_test_plans(plans)
    _poll_running_test_plans(plans)
    if full:
        _check_test_plans_for_liveness(plans)
        # After liveness, never before: that phase is the one with a deadline
        # the user feels (a plan going due, a push to a phone), and this one is
        # a label on a card. Both are budgeted, so the worst case is that a
        # slow remote costs the label a pass and not the other way round.
        _check_test_plan_landings(plans)
    # Re-read rather than reusing ``plans``: the phase above may have just folded
    # a finished run in, and sleeping four seconds because of the state it held
    # on the way IN would keep the fast cadence one tick longer than it is owed.
    try:
        return any(p["state"] == "running" for p in _test_plans.list_plans())
    except Exception:  # noqa: BLE001 — an unreadable store is not a reason to spin
        return False


# How old an unreferenced verify session must be before the sweeper may close
# it. The window it protects is run_test_plan's: the session exists from
# `create_instance` until `start_run` writes it onto the plan, and a sweep
# landing inside that gap would close a run the user started seconds ago. The
# gap is really milliseconds; fifteen minutes is deliberate overkill, because
# the sessions this exists for have been sitting unreferenced for DAYS.
_VERIFY_ORPHAN_GRACE_S = 15 * 60


async def _sweep_orphan_verify_sessions() -> None:
    """Close verify sessions that no plan remembers any more.

    The symmetric half of ``test_plans.prune``: prune releases a plan whose
    verify session is gone, and this closes a verify session whose plan is
    gone. Plans forget their sessions on paths that never reach
    ``_end_verify_session`` — ``ensure_plan_for`` replaces a plan wholesale
    when its session moves to a new branch, and the store's MAX_PLANS cap
    evicts old plans entirely — and the core module cannot end sessions itself
    (the engine owns them). What that stranded: an agent session, invisible in
    the rail (the sidebar hides ``verify-*`` on purpose), with no card left in
    the Verify dialog offering to end it, alive for days.

    A session is kept while ANY plan references it — the in-flight
    ``run_session`` or any run record's ``session`` (a finished run's session
    deliberately stays open for the user to read). Closing goes through
    ``_end_verify_session``: a close, not a delete, so a mistaken sweep is one
    click from coming back via recently-closed. Candidates are the sessions
    flagged ``hidden_by == "verify"``, never a title-prefix match.
    """
    try:
        plans = _test_plans.list_plans()
    except Exception:  # noqa: BLE001 — an unreadable store must not close anything
        return
    referenced = set()
    for plan in plans:
        title = str(plan.get("run_session") or "")
        if title:
            referenced.add(title)
        for run in plan.get("runs") or []:
            title = str((run or {}).get("session") or "")
            if title:
                referenced.add(title)
    for title in list(_server().ENGINE.instances.keys()):
        # Verify runs carry the hidden_by flag (set by run_test_plan, or by
        # the startup backfill for runs created before the flag existed); a
        # user's own `verify-foo` has no flag and is never swept.
        if _hidden_sessions.hidden_by(title) != "verify" or title in referenced:
            continue
        inst = _server().ENGINE.instances.get(title)
        created = getattr(inst, "CreatedAt", None)
        if created is None:
            # Unknown age — never close on a question we could not ask.
            continue
        try:
            age = (_datetime.datetime.now(created.tzinfo) - created).total_seconds()
        except Exception:  # noqa: BLE001 — a broken timestamp is not evidence of age
            continue
        if age < _VERIFY_ORPHAN_GRACE_S:
            continue
        try:
            if await _end_verify_session(title) and log.ErrorLog is not None:
                log.ErrorLog.Printf(
                    "verify: closed orphaned session %s (no plan references it)",
                    title,
                )
        except Exception:  # noqa: BLE001 — one bad session can't stop the sweep
            pass


async def _test_plans_due_loop() -> None:
    """Watch every waiting test plan until its work goes live, and poll the
    running ones (started by the lifespan).

    Work first, sleep after, like the autopilot loop: a plan whose PR merged
    while the server was down should come due within a minute of startup rather
    than after the first full interval.

    Two cadences, one loop. The full pass runs on its own minute regardless; in
    between, and only while something is actually running, the loop wakes on
    ``_TEST_PLAN_RUN_POLL_S`` to do the local half. The clock for the full pass
    is monotonic and independent of the fast ticks, so a long-running verify
    session cannot starve the liveness checks and a burst of fast ticks cannot
    bring a fetch storm forward.
    """
    next_full = 0.0
    while True:
        running = False
        try:
            full = time.monotonic() >= next_full
            if full:
                next_full = time.monotonic() + _TEST_PLAN_DUE_INTERVAL
            running = await asyncio.to_thread(_test_plans_due_pass, full)
            # On the minute, with the rest of the housekeeping — never on the
            # fast ticks, which exist only to read result files quickly.
            if full:
                await _sweep_orphan_verify_sessions()
        except Exception:  # noqa: BLE001 — the loop must never die
            pass
        await asyncio.sleep(
            _TEST_PLAN_RUN_POLL_S if running else _TEST_PLAN_DUE_INTERVAL
        )


def _closed_session_plan_inputs(title: str) -> tuple:
    """``(branch, sha, repo_root, program)`` for a session that is CLOSED, or
    ``(…, "")`` when there is nothing usable.

    WHY VERIFY HAS TO LOOK HERE. Everything else about this feature is built on
    the fact that a checklist outlives its session — that is why the plan stores
    the main repo rather than the worktree, and why a plan can be rewritten and
    run months after the work merged. Plan CREATION was the one half that still
    demanded a live window, so "write me a checklist for that" stopped being
    possible at exactly the moment people actually do it: after the work is done
    and the window has been closed.

    The recently-closed store already keeps what a plan needs — the title, the
    branch, and the repo the worktree was cut from — because it is the undo store
    for reopening the session, so nothing new has to be recorded.

    THE ONE THING IT CANNOT RECOVER is the intent. The ticket text lives in the
    session's seed prompt, the closed entry does not carry it, and the transcript
    that would hold it lived in a worktree that is reclaimed on close. So a
    checklist written from here is diff-only, and it says so rather than
    pretending: the honest fix for a diff-only draft is the rewrite box, where a
    person can type what it should have been about in one sentence.
    """
    for entry in _server()._load_recently_closed():
        if str(entry.get("title") or "") != title:
            continue
        data = entry.get("data") or {}
        wt = data.get("worktree") or {}
        repo_root = (
            str(wt.get("repo_path") or "")
            or str(entry.get("folder") or "")
            or str(data.get("path") or "")
        )
        branch = str(entry.get("branch") or data.get("branch") or "")
        program = str(data.get("program") or "")
        if not repo_root or not branch:
            return "", "", "", ""
        # The branch may well be gone locally (merged and deleted), so origin's
        # copy is a real answer and not a fallback. Whichever resolves is the
        # commit the checklist is about.
        sha = ""
        for ref in (branch, "origin/%s" % branch):
            out = subprocess.run(
                ["git", "-C", repo_root, "rev-parse", "--verify", "-q", ref],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=15,
            )
            if out.returncode == 0:
                sha = out.stdout.decode("utf-8", "replace").strip()
                break
        return branch, sha, repo_root, program
    return "", "", "", ""


async def _write_test_plan_for_closed(title: str) -> JSONResponse:
    """`instance_write_test_plan` for a session that has been closed.

    The same answers in the same shapes — ``existing: true`` for one that is
    already written, 202 for one being written — because the caller is one
    button and must not have to care which store the session came from.

    Generation runs with NO worktree, which is the path `generate` already takes
    for every rewrite of a plan whose session is gone: it falls back to the
    plan's ``repo_root``, reads the branch's diff there, and asks the repo's
    default CLI. That is exactly the situation here.
    """

    def _prepare():
        branch, sha, repo_root, program = _closed_session_plan_inputs(title)
        if not repo_root:
            return (
                None,
                "",
                "",
                JSONResponse({"error": "no such session: %s" % title}, status_code=404),
            )
        if not branch:
            return (
                None,
                "",
                "",
                JSONResponse(
                    {"error": "that session wasn't on a branch"}, status_code=409
                ),
            )
        if not sha:
            return (
                None,
                "",
                "",
                JSONResponse(
                    {
                        "error": "%s is gone from this repo — nothing left to write a "
                        "checklist from" % branch
                    },
                    status_code=409,
                ),
            )
        plan = _test_plans.ensure_plan_for(
            title,
            branch,
            sha,
            repo_root,
            _test_plans.resolve_live_branch(repo_root),
            # Deliberately empty: the closed entry does not carry the seed
            # prompt and the transcript went with the worktree. Guessing here
            # would be worse than a diff-only draft the user can aim with the
            # rewrite box.
            intent="",
        )
        # Same lookup as the live route: the checklist that already covers this
        # branch may be keyed by another window that was on it.
        owner = (
            ""
            if plan is not None
            else _test_plans.owner_for_branch(title, repo_root, branch)
        )
        return plan, program, owner, None

    plan, program, owner, resp = await asyncio.to_thread(_prepare)
    if resp is not None:
        return resp
    if plan is None:
        existing = _test_plans.get(owner) or {}
        return JSONResponse(
            {
                "ok": True,
                "plan": owner or title,
                "existing": True,
                "state": existing.get("state", ""),
            }
        )
    _start_test_plan_generation(title, program, "")
    return JSONResponse({"ok": True, "plan": title, "existing": False}, status_code=202)


async def _end_verify_session(title: str) -> bool:
    """End the verify session ``title`` if it is live. Returns whether it was.

    ``close`` rather than DELETE, deliberately: a verify workspace is cheap but
    it is not ours to destroy — the agent may have left notes, a log tail, a
    half-finished repro in it, and the run being cancelled is often exactly when
    someone wants to look. Closing keeps the worktree and puts the session in
    the recently-closed store, so it is one click from coming back.
    """
    if not title or title not in _server().ENGINE.instances:
        return False
    resp = await _server().close_instance(title)
    return resp.status_code < 400
