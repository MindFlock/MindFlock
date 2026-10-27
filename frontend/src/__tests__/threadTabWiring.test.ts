/** The Thread tab's WIRING (grid/Pane.tsx → grid/ThreadTab.tsx, plus the
 * lib/keymap guards and the session action table rows that reach it). Moved
 * from tests/unit/test_frontend_thread.py, which grepped the built bundle;
 * these pin the owning source module instead (comments stripped, whitespace
 * normalised), and the pure half through behaviour. thread.test.ts covers the
 * pure half in depth; what is here is the rest of the old Python contract.
 *
 * The rules that matter most are pinned by what is ABSENT as much as by what
 * is present: the log never marks mail read (it reads only `/thread`, never
 * the consuming `/messages` route), the composer never sends through the
 * agents' mailbox, a recipient on a dialog is never typed into, and no new
 * module calls `prompt`/`confirm`/`alert` (dead in Electron). */

/// <reference types="vite/client" />
import { describe, expect, it, vi } from "vitest";
import { dropComments, norm } from "./sourceText";
import type { Instance } from "../api/types";
import PANE_RAW from "../components/grid/Pane.tsx?raw";
import THREAD_TAB_RAW from "../components/grid/ThreadTab.tsx?raw";
import THREAD_LIB_RAW from "../lib/thread.ts?raw";
import KEYMAP_RAW from "../lib/keymap.ts?raw";
import FLOCK_ACTIONS_RAW from "../lib/flockActions.ts?raw";
import CSS_RAW from "../components/grid/ThreadTab.css?raw";
import LIGHT_CSS_RAW from "../styles/theme-light.css?raw";

const {
  NOT_FREE,
  claimComposeRequest,
  composeChips,
  composePlaceholder,
  deliveryText,
  headerSummary,
  logEntries,
  sendPlan,
  threadBadge,
  threadTabShown,
  workerRows,
} = await import("../lib/thread");
const { SESSION_ACTIONS } = await import("../lib/sessionActionTable");
const { openThread } = await import("../lib/flockActions");
const { THREAD_POLL_MS } = await import("../components/grid/ThreadTab");
const { useUi } = await import("../state/store");

/** Source minus comments (a comment that mentions `/messages` is not a call
 * to it), layout-blind (`norm`) — the formatter's line breaks are layout, the
 * way tests/_bundle.squash treated the bundler's. */
const code = (raw: string) => norm(dropComments(raw));
const PANE = code(PANE_RAW);
const TAB = code(THREAD_TAB_RAW);
const LIB = code(THREAD_LIB_RAW);
const KEYMAP = code(KEYMAP_RAW);

/** One top-level function's text, from its `function name(` to the closing
 * brace at column 0. */
function fn(raw: string, name: string): string {
  const m = new RegExp("^(?:export )?(?:async )?function " + name + "\\(", "m").exec(raw);
  expect(m, `function ${name}`).toBeTruthy();
  const end = raw.indexOf("\n}", m!.index);
  return code(raw.slice(m!.index, end + 2));
}

const row = (o: Partial<Instance>): Instance => ({ title: "x", activity: "idle", ...o }) as Instance;
const act = (r: Partial<Instance>) => String(r.activity || "idle");

describe("the tab on the pane", () => {
  it("sits right after Queue and only for a family (or opened on purpose)", () => {
    const queue = PANE.indexOf('data-tab="queue"');
    const thread = PANE.indexOf('data-tab="thread"');
    expect(queue).toBeGreaterThanOrEqual(0);
    expect(thread).toBeGreaterThan(queue);
    expect(PANE).toContain('className={"thread-tab" + (tab === "thread" ? " active" : "")}');
    // Shown for a session with a parent or workers, a group's lead (its plan
    // and its one PR live there before any worker exists), or opened on purpose.
    expect(PANE).toContain(
      'threadTabShown(!!family.parent || family.children.length > 0 || inst.run?.role === "lead", ' +
        'threadOpened || lastTab === "thread")'
    );
    expect(PANE).toContain("{threadShown && (");
    expect(PANE).toContain("threadOpened = useUi((s) => s.threadComposeTarget?.title === title)");
    // threadTabShown is exactly hasFamily || opened.
    expect(fn(THREAD_LIB_RAW, "threadTabShown")).toContain("return hasFamily || opened;");
    for (const [fam, opened] of [[false, false], [true, false], [false, true], [true, true]] as const)
      expect(threadTabShown(fam, opened)).toBe(fam || opened);
    // And it has a body: "thread" is a tab the pane draws.
    const bodyTabs = /const BODY_TABS[^;]*;/.exec(PANE)?.[0] || "";
    expect(bodyTabs).toContain('"thread"');
    expect(PANE).toContain('className={"pane-thread" + (tab !== "thread" ? " hidden" : "")}');
    expect(PANE).toContain('active={tab === "thread"}');
  });

  it("the badge reuses the queue badge", () => {
    expect(PANE).toContain('className={"queue-tab-badge thread-badge" + (badge.needs ? " needs" : "")}');
    expect(PANE).toContain("threadBadge(family.children, threadSeen, effectiveActivity)");
    // Looking at the tab is what "seen" means — on open and per new report.
    expect(PANE).toContain('if (tab === "thread") useUi.getState().setThreadLastSeen(title);');
    const badge = fn(THREAD_LIB_RAW, "threadBadge");
    expect(badge).toContain('=== "clarify") needs++;');
    // Report ts is epoch SECONDS, lastSeen epoch MS.
    expect(badge).toContain("Number(r.ts) * 1000 > (lastSeenMs || 0)");
    const kids = [
      row({ title: "a", activity: "clarify" }),
      row({ title: "b", last_report: { id: "r", status: "done", summary: "", ts: 100 } } as Partial<Instance>),
    ];
    expect(threadBadge(kids, 99_000, act)).toEqual({ count: 2, needs: 1, fresh: 1 });
    expect(threadBadge(kids, 100_000, act)).toEqual({ count: 1, needs: 1, fresh: 0 });
  });
});

describe("worker rows", () => {
  it("answer, decide, review and merge", () => {
    // Needs-you rows host the shared answer strip, Thread variant, with
    // "Let <t> decide" in its children slot.
    expect(TAB).toContain('variant="thread"');
    expect(TAB).toContain('className="fa-decide"');
    expect(TAB).toContain("Let {parentName} decide");
    // Let api decide QUEUES a short prompt to the orchestrator, naming the
    // worker by its TITLE (review 2026-10-05: it used the alias, which the
    // agent can't address — or that names another session).
    expect(TAB).toContain('await instApi(title, "/queue", { json: { text: decidePrompt(worker,');
    expect(TAB).not.toContain("decidePrompt(nameOf(");
    // A "No…" answer re-addresses the composer to that worker.
    expect(TAB).toContain("onRedirect={() => address(w.title)}");
    // Review diff = the worker's own Diff tab; Map = its Map, on what it changed.
    expect(TAB).toContain("useUi.getState().openDiff(row.title)");
    expect(TAB).toContain('useUi.getState().openMap(row.title, { section: "changed" })');
    // Merge into <t> pastes the wrapup playbook scoped to that worker.
    expect(TAB).toContain('paste("merge:" + w.title, "wrapup", { only: w.title }');
    // Header buttons are paste actions too.
    expect(TAB).toContain('paste("workers", "workers")');
    expect(TAB).toContain('paste("wrapup", "wrapup")');
    expect(fn(THREAD_LIB_RAW, "workerRows")).toContain("RANK[a.r.state] - RANK[b.r.state]");
    const rows = workerRows(
      [row({ title: "w1", activity: "working" }), row({ title: "w2", activity: "clarify" })],
      [],
      act
    );
    expect(rows.map((r) => r.title)).toEqual(["w2", "w1"]);
  });

  it("header copy", () => {
    expect(TAB).toContain("{myName}'s workers");
    expect(TAB).toContain("Buttons below either answer directly or paste a prompt into {myName} — nothing is typed");
    expect(TAB).toContain("Check on workers");
    expect(TAB).toContain("Wrap up ({reported} reported)");
    const summary = fn(THREAD_LIB_RAW, "headerSummary");
    expect(summary).toContain('" forked from "');
    expect(summary).toContain('" you"');
    const base = { activity: "", activitySince: 0, report: null, diff: null, stage: "", waitDetail: "" };
    const parts = headerSummary([
      { ...base, title: "a", state: "ask", baseSha: "3f2c1a0aa" },
      { ...base, title: "b", state: "working", baseSha: "3f2c1a0aa" },
    ]);
    expect(parts.map((p) => p.text)).toEqual(["2 workers forked from ", "3f2c1a0", "1 needs you", "1 working"]);
  });
});

describe("the log is read-only", () => {
  it("reads the thread and never marks mail read", () => {
    expect(TAB).toContain('instApi<unknown>(title, "/thread?limit=50")');
    expect(TAB).toContain('"/thread?limit=50&before=" + encodeURIComponent(first.id)');
    for (const mod of [TAB, LIB]) {
      // The consuming inbox route and any "mark read" write are absent.
      expect(mod).not.toContain('"/messages');
      expect(mod).not.toContain("mark_read");
      expect(mod).not.toContain("/read");
    }
    // Refreshed by session.message for a family member, and polled while open.
    expect(TAB).toContain('ev?.subscribe("session.message", (env) => {');
    expect(TAB).toContain("if (!fam.has(env.session) && !fam.has(from)) return;");
    expect(TAB).toContain('document.visibilityState === "visible"');
    expect(THREAD_POLL_MS).toBe(8000);
    expect(TAB).toContain("window.setInterval(tick, THREAD_POLL_MS)");
    // All | Reports.
    expect(fn(THREAD_LIB_RAW, "logEntries")).toContain(
      'if (filter === "reports" && it.type !== "result") continue;'
    );
    const items = [
      { id: "1", type: "message", from: "a", to: "b", ts: 1 },
      { id: "2", type: "result", from: "b", to: "a", ts: 2 },
    ] as unknown as Parameters<typeof logEntries>[0];
    expect(logEntries(items, "reports").map((e) => e.key)).toEqual(["2"]);
    expect(fn(THREAD_LIB_RAW, "deliveryText")).toContain('"read by " + to');
    expect(deliveryText("read", "api")).toBe("read by api");
  });
});

describe("the composer", () => {
  it("sends as you and never into a dialog", () => {
    // Send now -> /send, When idle / not free -> /queue: the Queue tab's
    // routes. Send now is dialog_safe: the SERVER re-checks the agent and
    // queues instead of typing into a prompt (review 2026-10-05).
    expect(TAB).toContain(
      '...plannedNow.map((t) => instApi<{ queued?: boolean }>(t, "/send", { json: body })), ' +
        '...plannedLater.map((t) => instApi(t, "/queue", { json: { text } }))'
    );
    // `say` only with Read replies aloud on Everything (lib/voiceDesktop sayOnSend).
    expect(TAB).toContain("const body = sayOnSend(false) ? { text, dialog_safe: true, say: true } : { text, dialog_safe: true };");
    expect(TAB).toMatch(/!!\(r\.value as \{ queued\?: boolean \} \| null\)\?\.queued/);
    // Never the agents' mailbox.
    expect(TAB).not.toContain("/message");
    expect(TAB).not.toContain("send_message");
    // Ctrl+Enter sends now.
    expect(TAB).toContain('if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {');
    expect(TAB).toContain('send("now")');
    // A recipient on a dialog or at the limit is queued, and with nobody
    // free the button reads "When it's free".
    const plan = fn(THREAD_LIB_RAW, "sendPlan");
    expect(plan).toContain("(NOT_FREE.has(actOf(t)) ? later : now).push(t)");
    expect(plan).toContain('label: now.length ? "Send now" : "When it\'s free"');
    expect([...NOT_FREE].sort()).toEqual(["clarify", "limit"]);
    expect(LIB).toContain('new Set(["clarify", "limit"])');
    const acts: Record<string, string> = { a: "clarify", b: "limit", c: "working" };
    expect(sendPlan(["a", "b", "c"], (t) => acts[t])).toEqual({ now: ["c"], later: ["a", "b"], label: "Send now" });
    expect(sendPlan(["a", "b"], (t) => acts[t]).label).toBe("When it's free");
    expect(fn(THREAD_LIB_RAW, "composePlaceholder")).toContain("typed into its prompt as you");
    expect(composePlaceholder({ key: "w", label: "w", titles: ["w"] })).toContain("typed into its prompt as you");
    expect(fn(THREAD_LIB_RAW, "composeChips")).toContain('"all workers"');
    expect(composeChips("api", "", ["w1", "w2"], (t) => t).map((c) => c.label)).toContain("all workers");
    expect(TAB).toContain('className="thread-input"');
  });

  it("threadOpen addresses the composer and takes the caret", () => {
    expect(TAB).toContain(
      "useUi((s) => (s.threadComposeTarget?.title === title ? s.threadComposeTarget : null))"
    );
    expect(TAB).toContain("setToKey(target.to || title);");
    expect(TAB).toContain("el.focus();");
    // Every entry point reaches the real tab (no select-the-pane fallback).
    const open = fn(FLOCK_ACTIONS_RAW, "openThread");
    expect(open).toContain("useUi.getState().threadOpen(title, composeTo ? { composeTo } : undefined)");
    expect(open).not.toContain("selectSession");
    const threadOpen = vi.fn();
    const prev = useUi.getState().threadOpen;
    useUi.setState({ threadOpen } as never);
    try {
      openThread("api", "w1");
      openThread("api");
      expect(threadOpen.mock.calls).toEqual([["api", { composeTo: "w1" }], ["api", undefined]]);
      // Ctrl+K S / T are the table's Message… and Thread rows.
      const msg = SESSION_ACTIONS.message;
      expect([msg.label, msg.keys, msg.chord]).toEqual(["Message…", "Ctrl+K S", "s"]);
      expect(msg.enabled()).toBe(true);
      const thr = SESSION_ACTIONS.thread;
      expect([thr.label, thr.keys, thr.chord, thr.note]).toEqual([
        "Thread",
        "Ctrl+K T",
        "t",
        "workers and messages",
      ]);
      expect(thr.enabled()).toBe(true);
      threadOpen.mockClear();
      msg.run("w2");
      thr.run("w3");
      expect(threadOpen.mock.calls).toEqual([["w2", { composeTo: "w2" }], ["w3"]]);
    } finally {
      useUi.setState({ threadOpen: prev } as never);
    }
  });

  it("Ctrl+W in the composer never ends the session", () => {
    expect(fn(KEYMAP_RAW, "threadComposerFocused")).toContain(
      'isEditingTarget(el) && !!el.closest?.(".thread-compose")'
    );
    // Both end shortcuts consult one guard (endShortcutAllowed): no overlay,
    // no typing target, not inside the Thread tab — the composer included.
    expect(KEYMAP).toContain("when: () => endShortcutAllowed() && !threadComposerFocused()");
    const all = fn(KEYMAP_RAW, "endShortcutAllowed");
    expect(all).toContain("typingTarget(active)");
    expect(all).toContain("threadTabFocused()");
    expect(TAB).toContain('className="thread-compose"');
  });

  it("a compose request is handled once (review 2026-10-05)", () => {
    // threadComposeTarget re-applied on every tab switch and remount —
    // readdressing the composer and stealing the caret.
    expect(TAB).toContain("!claimComposeRequest(title, seq)");
    expect(TAB).toContain("toKeys.set(title, to)");
    expect(fn(THREAD_LIB_RAW, "claimComposeRequest")).toContain("composeHandled.set(title, seq)");
    expect(claimComposeRequest("wiring-test", 3)).toBe(true);
    expect(claimComposeRequest("wiring-test", 3)).toBe(false);
  });

  it("Delete never ends the session from the Thread tab (review 2026-10-05)", () => {
    expect(fn(KEYMAP_RAW, "threadTabFocused")).toContain('closest?.(".thread-root")');
    const all = fn(KEYMAP_RAW, "endShortcutAllowed");
    expect(all).toContain('closest?.(".cm-root")');
    expect(all).toContain("threadTabFocused()");
    expect(KEYMAP).toContain('closest?.(".cm-root")');
    // The Thread tab is refused inside the shared guard, the composer on top.
    expect(KEYMAP).toContain("endShortcutAllowed() && !threadComposerFocused()");
  });
});

describe("the new modules", () => {
  it("never call a dead dialog (prompt/confirm/alert are no-ops in Electron)", () => {
    const dead = /(?<![\w.$])(?:window\.)?(?:prompt|confirm|alert)\(/;
    for (const [name, src] of [
      ["lib/thread.ts", THREAD_LIB_RAW],
      ["components/grid/ThreadTab.tsx", THREAD_TAB_RAW],
    ]) {
      expect(dead.test(src), name).toBe(false);
      expect(dead.test(code(src)), name).toBe(false);
    }
  });

  it("styles", () => {
    const css = (CSS_RAW + "\n" + LIGHT_CSS_RAW).replace(/\s+/g, " ");
    for (const sel of [
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
    ])
      expect(css, sel).toContain(sel);
  });
});
