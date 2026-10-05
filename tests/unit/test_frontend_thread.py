"""MindFlock MCP from the UI, the Thread tab (unit F3): structural checks.

The pane's Thread tab (grid/ThreadTab.tsx, after Queue): a session's workers
with their dialogs, reports and the paste actions, the read-only "Between
sessions" log, and the composer that types into a member's prompt as YOU.
lib/thread.ts's pure half and the Ctrl+W / Delete guard are pinned by vitest
(thread.test.ts), and the rendered tab was checked in the screenshot harness.
These tests pin the COMMITTED bundle so the wiring can't regress silently.

The rules that matter most are pinned by what is ABSENT as much as by what
is present: the log never marks mail read (it reads only ``/thread``, never
the consuming ``/messages`` route), the composer never sends through the
agents' mailbox, a recipient on a dialog is never typed into, and no new
module calls ``prompt``/``confirm``/``alert`` (dead in Electron).

Multi-token snippets go through ``tests/_bundle.py`` (Vite 8 / Rolldown lays
the bundle out with tabs and line breaks).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._bundle import in_bundle

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "backend" / "web" / "static"

DEAD_DIALOG = re.compile(r"(?<![\w.$])(?:window\.)?(?:prompt|confirm|alert)\(")

F3_MODULES = (
    "src/lib/thread.ts",
    "src/components/grid/ThreadTab.tsx",
)


@pytest.fixture(scope="module")
def js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return (STATIC / "style.css").read_text(encoding="utf-8")


def _region(js: str, module: str) -> str:
    """The bundled text of one source module (Rolldown's ``//#region`` markers)."""
    start = js.find("//#region " + module + "\n")
    assert start >= 0, f"{module} is not in the bundle"
    end = js.find("//#endregion", start)
    assert end > start
    return js[start:end]


def _function(js: str, name: str) -> str:
    """The text of a top-level bundled function, up to the next top-level one."""
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\(", js, re.M)
    assert m, f"function {name} not in bundle"
    nxt = re.search(r"^(?:async )?function |^//#endregion", js[m.end() :], re.M)
    return js[m.start() : m.end() + (nxt.start() if nxt else len(js))]


# --- The tab on the pane --------------------------------------------------------


def test_the_tab_sits_after_queue_and_only_for_a_family(js):
    pane = _region(js, "src/components/grid/Pane.tsx")
    queue = pane.find('"data-tab": "queue"')
    thread = pane.find('"data-tab": "thread"')
    assert 0 <= queue < thread, "the Thread tab comes right after Queue"
    assert in_bundle(
        'className: "thread-tab" + (tab === "thread" ? " active" : "")', pane
    )
    # Shown for a session with a parent or workers, or opened on purpose.
    assert in_bundle(
        'threadTabShown(!!family.parent || family.children.length > 0, threadOpened || lastTab === "thread")',
        pane,
    )
    assert "threadShown && " in pane
    assert in_bundle(
        "threadOpened = useUi((s) => s.threadComposeTarget?.title === title)", pane
    )
    shown = _function(js, "threadTabShown")
    assert "return hasFamily || opened;" in shown
    # And it has a body now: "thread" is a tab the pane draws.
    body_tabs = pane[pane.find("var BODY_TABS = ") :]
    body_tabs = body_tabs[: body_tabs.find(";")]
    assert '"thread"' in body_tabs
    assert in_bundle(
        'className: "pane-thread" + (tab !== "thread" ? " hidden" : "")', pane
    )
    assert in_bundle('active: tab === "thread"', pane)


def test_the_badge_reuses_the_queue_badge(js):
    pane = _region(js, "src/components/grid/Pane.tsx")
    assert in_bundle(
        'className: "queue-tab-badge thread-badge" + (badge.needs ? " needs" : "")',
        pane,
    )
    assert in_bundle(
        "threadBadge(family.children, threadSeen, effectiveActivity)", pane
    )
    # Looking at the tab is what "seen" means — on open and per new report.
    assert in_bundle(
        'if (tab === "thread") useUi.getState().setThreadLastSeen(title);', pane
    )
    badge = _function(js, "threadBadge")
    # (Rolldown inlines the source's `act` local; don't pin its spelling.)
    assert '=== "clarify") needs++;' in badge
    # Report ts is epoch SECONDS, lastSeen epoch MS.
    assert in_bundle("Number(r.ts) * 1e3 > (lastSeenMs || 0)", badge)


# --- Worker rows ---------------------------------------------------------------------


def test_worker_rows_answer_decide_review_and_merge(js):
    tab = _region(js, "src/components/grid/ThreadTab.tsx")
    # Needs-you rows host the shared answer strip, Thread variant, with
    # "Let <t> decide" in its children slot and Open ↗.
    assert in_bundle('variant: "thread"', tab)
    assert 'className: "fa-decide"' in tab
    assert "Let " in tab and "decide" in tab
    # Let api decide QUEUES a short prompt to the orchestrator.
    # ...naming the worker by its TITLE (review 2026-10-05: it used the
    # alias, which the agent can't address — or that names another session).
    assert in_bundle(
        'await instApi(title, "/queue", { json: { text: decidePrompt(worker,', tab
    )
    assert "decidePrompt(nameOf(" not in tab
    # A "No…" answer re-addresses the composer to that worker.
    assert "onRedirect: () => address(w.title)" in tab
    # Review diff = the worker's own Diff tab.
    assert in_bundle('useUi.getState().setLastTab(row.title, "diff")', tab)
    # Merge into <t> pastes the wrapup playbook scoped to that worker.
    assert in_bundle('paste("merge:" + w.title, "wrapup", { only: w.title }', tab)
    # Header buttons are paste actions too.
    assert in_bundle('paste("workers", "workers")', tab)
    assert in_bundle('paste("wrapup", "wrapup")', tab)
    rows = _function(js, "workerRows")
    assert in_bundle("RANK[a.r.state] - RANK[b.r.state]", rows)


def test_header_copy(js):
    tab = _region(js, "src/components/grid/ThreadTab.tsx")
    assert "'s workers" in tab
    assert "Buttons below either answer directly or paste a prompt into " in tab
    assert "nothing is typed" in tab
    assert "Check on workers" in tab
    assert "Wrap up (" in tab and " reported)" in tab
    summary = _function(js, "headerSummary")
    assert '" forked from "' in summary
    assert '" your answer"' in summary


# --- The log is read-only -------------------------------------------------------------


def test_the_log_reads_the_thread_and_never_marks_mail_read(js):
    tab = _region(js, "src/components/grid/ThreadTab.tsx")
    lib = _region(js, "src/lib/thread.ts")
    assert 'instApi(title, "/thread?limit=50")' in tab
    assert in_bundle('"/thread?limit=50&before=" + encodeURIComponent(first.id)', tab)
    for mod in (tab, lib):
        # The consuming inbox route and any "mark read" write are absent.
        assert '"/messages' not in mod
        assert "mark_read" not in mod
        assert "/read" not in mod
    # Refreshed by session.message for a family member, and polled while open.
    assert in_bundle('ev?.subscribe("session.message", (env) => {', tab)
    assert "if (!fam.has(env.session) && !fam.has(from)) return;" in tab
    assert 'document.visibilityState === "visible"' in tab
    assert "var THREAD_POLL_MS = 8e3;" in tab
    # All | Reports.
    entries = _function(js, "logEntries")
    assert in_bundle(
        'if (filter === "reports" && it.type !== "result") continue;', entries
    )
    delivery = _function(js, "deliveryText")
    assert '"read by " + to' in delivery


# --- The composer ----------------------------------------------------------------------


def test_the_composer_sends_as_you_and_never_into_a_dialog(js):
    tab = _region(js, "src/components/grid/ThreadTab.tsx")
    # Send now -> /send, When idle / not free -> /queue: the Queue tab's routes.
    # Send now is dialog_safe: the SERVER re-checks the agent and queues
    # instead of typing into a prompt (review 2026-10-05) — this tab's view
    # of the activity is a poll or two behind.
    assert in_bundle(
        '...plannedNow.map((t) => instApi(t, "/send", { json: { text, dialog_safe: true } })), ...plannedLater.map((t) => instApi(t, "/queue", { json: { text } }))',
        tab,
    )
    assert "!!r.value?.queued" in tab
    # Never the agents' mailbox.
    assert "/message" not in tab
    assert "send_message" not in tab
    # Ctrl+Enter sends now.
    assert in_bundle('if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {', tab)
    assert 'send("now")' in tab
    # A recipient on a dialog or at the limit is queued, and with nobody free
    # the button reads "When it's free".
    plan = _function(js, "sendPlan")
    assert in_bundle("(NOT_FREE.has(actOf(t)) ? later : now).push(t)", plan)
    assert in_bundle('label: now.length ? "Send now" : "When it\'s free"', plan)
    assert in_bundle('new Set(["clarify", "limit"])', _region(js, "src/lib/thread.ts"))
    ph = _function(js, "composePlaceholder")
    assert "typed into its prompt as you" in ph
    chips = _function(js, "composeChips")
    assert '"all workers"' in chips
    assert 'className: "thread-input"' in tab


def test_threadopen_addresses_the_composer_and_takes_the_caret(js):
    tab = _region(js, "src/components/grid/ThreadTab.tsx")
    assert in_bundle(
        "useUi((s) => s.threadComposeTarget?.title === title ? s.threadComposeTarget : null)",
        tab,
    )
    assert "setToKey(target.to || title);" in tab
    assert "el.focus();" in tab
    # Every entry point reaches the real tab (no select-the-pane fallback).
    open_thread = _function(js, "openThread")
    # (`undefined` is spelled `void 0` by the bundler.)
    assert in_bundle(
        "useUi.getState().threadOpen(title, composeTo ? { composeTo } : void 0)",
        open_thread,
    )
    assert "selectSession" not in open_thread
    assert in_bundle(
        's: { desc: "Message…", run: (t) => useUi.getState().threadOpen(t, { composeTo: t }) }',
        js,
    )
    assert in_bundle(
        't: { desc: "Thread — workers and messages", run: (t) => useUi.getState().threadOpen(t) }',
        js,
    )


def test_ctrl_w_in_the_composer_never_ends_the_session(js):
    guard = _function(js, "threadComposerFocused")
    assert in_bundle('isEditingTarget(el) && !!el.closest?.(".thread-compose")', guard)
    assert in_bundle(
        "when: () => !!useUi.getState().focused && !modalOpen() && !threadComposerFocused()",
        js,
    )
    # Delete is covered by the editing-target guard it already had.
    assert in_bundle("!isEditingTarget(document.activeElement) &&", js)
    tab = _region(js, "src/components/grid/ThreadTab.tsx")
    assert 'className: "thread-compose"' in tab


def test_new_modules_never_call_a_dead_dialog(js):
    for mod in F3_MODULES:
        hits = DEAD_DIALOG.findall(_region(js, mod))
        assert not hits, f"{mod} calls {hits}, a no-op in Electron"
        src = (ROOT / "frontend" / mod).read_text(encoding="utf-8")
        assert not DEAD_DIALOG.search(src), mod


def test_styles(css):
    for sel in (
        ".pane-head .tabs button.thread-tab.active .queue-tab-badge",
        ".queue-tab-badge.needs",
        ".pane-thread.hidden",
        ".thread-workers",
        ".th-worker.is-ask",
        ".th-card.k-result",
        ".thread-compose",
        ".th-chip.on",
        ".light .thread-root .flock-answer.fa-thread",
        ".light .pane-head .tabs button:not(.active):hover",
    ):
        assert sel in css, sel


def test_review_a_compose_request_is_handled_once(js):
    """Review 2026-10-05: threadComposeTarget re-applied on every tab switch
    and remount — readdressing the composer and stealing the caret."""
    tab = _region(js, "src/components/grid/ThreadTab.tsx")
    assert "!claimComposeRequest(title, seq)" in tab
    assert "toKeys.set(title, to)" in tab
    claim = _function(js, "claimComposeRequest")
    assert "composeHandled.set(title, seq)" in claim


def test_review_delete_never_ends_the_session_from_the_thread_tab(js):
    keymap = _region(js, "src/lib/keymap.ts")
    assert 'closest?.(".thread-root")' in _function(js, "threadTabFocused")
    assert in_bundle(
        '!document.activeElement?.closest?.(".cm-root") && !threadTabFocused()', keymap
    )
    assert in_bundle("!threadComposerFocused() && !threadTabFocused()", keymap)
