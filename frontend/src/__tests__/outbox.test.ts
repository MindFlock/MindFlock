/** The Outbox as data (components/outbox/outbox.ts): one response, filtered
 * per tab, so a tab's badge is the length of exactly what the tab shows — and
 * one branch is one row however many windows sit on it. */
import { describe, it, expect } from "vitest";
import type { Instance, OutboxResponse } from "../api/types";
import {
  approvalKey,
  dedupe,
  messageHead,
  needsAttention,
  outboxTabs,
  shippedChip,
  shipVerb,
  statText,
  thenText,
  viewCount,
  viewFor,
  waitingChip,
  waitingCount,
} from "../components/outbox/outbox";

const run = { id: "r1", name: "Q4 payments" };
const DATA: OutboxResponse = {
  counts: { waiting: 2, shipping: 2, shipped: 2, queued: 2 },
  groups: {
    waiting: [
      { key: "q::f/419", title: "jira-PAY-419", run: { ...run, task: "t3" }, kind: "prompt", reason: "its agent is asking" },
      {
        key: "w::you/dark",
        title: "web-dark-mode",
        run: null,
        kind: "approve",
        step: "commit",
        preview: { commit_message: "web: add a dark theme toggle", pr_title: null, files: 5, add: 210, del: 32 },
        actions: ["ship", "diff", "edit_message"],
      },
    ],
    // No `run` on these: the group is read off the session's row.
    shipping: [
      { title: "q4-ratelimit", step: "make_pr", note: "opening PR", lane: "pr" },
      { title: "lonely", step: "push", note: "pushing", lane: "push" },
    ],
    shipped: [
      { key: "q::f/412", title: "jira-PAY-412", pr_url: "https://github.com/q/q/pull/318", pr_state: "open", checks: "pass", commit_subject: "refunds: retry" },
      // The same branch from its duplicate window: one row, not two.
      { key: "q::f/412", title: "jira-PAY-412-copy", pr_url: "https://github.com/q/q/pull/318", pr_state: "open", checks: "pass" },
    ],
    queued: [
      { run: { ...run, task: "t5" }, ref: "PAY-421", text: "Upgrade to Stripe API 2026-09" },
      { run: { ...run, task: "t6" }, text: "Backfill the refund ledger" },
    ],
  },
  summaries: [{ run: "r0", name: "Auth cleanup", text_md: "## Auth cleanup\n- 3 PRs" }],
};
const ROWS: Record<string, Partial<Instance>> = {
  "q4-ratelimit": { run: { ...run, task: "t2", role: "task", grouping: "each" } },
  "jira-PAY-412": { run: { ...run, task: "t1", role: "task", grouping: "each" } },
};
const rowOf = (t: string) => ROWS[t] as Instance | undefined;

describe("Outbox tabs: badges count what the tab shows", () => {
  it("dedupes one branch to one row", () => {
    expect(dedupe(DATA.groups.shipped).map((s) => s.title)).toEqual(["jira-PAY-412"]);
    // No key: falls back to the title.
    expect(dedupe([{ title: "a" }, { title: "a" }, { title: "b" }]).length).toBe(2);
  });

  it("filters to a group, using the session's own run when the row has none", () => {
    const g = viewFor(DATA, "r1", rowOf);
    expect(g.waiting.map((w) => w.title)).toEqual(["jira-PAY-419"]);
    expect(g.shipping.map((s) => s.title)).toEqual(["q4-ratelimit"]);
    expect(g.shipped.map((s) => s.title)).toEqual(["jira-PAY-412"]);
    expect(g.queued).toHaveLength(2);
    const own = viewFor(DATA, "own", rowOf);
    expect(own.waiting.map((w) => w.title)).toEqual(["web-dark-mode"]);
    expect(own.shipping.map((s) => s.title)).toEqual(["lonely"]);
    expect(own.queued).toEqual([]);
  });

  it("builds All · one chip per group · On their own, each counting its own rows", () => {
    const tabs = outboxTabs(DATA, rowOf);
    // What waits on you is the bell's: no chip counts it.
    expect(tabs.map((t) => [t.key, t.label, t.count])).toEqual([
      ["all", "All", 5],
      ["r1", "Q4 payments", 4],
      ["own", "On their own", 1],
    ]);
    for (const t of tabs) expect(t.count).toBe(viewCount(viewFor(DATA, t.key, rowOf)));
  });

  it("has no On their own tab when there is no group, and nothing at all without data", () => {
    const solo: OutboxResponse = { ...DATA, groups: { waiting: [DATA.groups.waiting[1]], shipping: [], shipped: [], queued: [] } };
    expect(outboxTabs(solo, () => undefined).map((t) => t.key)).toEqual(["all"]);
    expect(outboxTabs(null, rowOf).map((t) => [t.key, t.count])).toEqual([["all", 0]]);
  });

  it("leaves what waits on you out of every count, but keeps it in the view", () => {
    const all = viewFor(DATA, "all", rowOf);
    expect(all.waiting).toHaveLength(2);
    expect(viewCount(all)).toBe(5);
    // A group whose only rows wait on you gets no chip.
    const waitOnly: OutboxResponse = {
      ...DATA,
      groups: { waiting: [DATA.groups.waiting[0]], shipping: [], shipped: [], queued: [] },
    };
    expect(outboxTabs(waitOnly, rowOf).map((t) => [t.key, t.count])).toEqual([["all", 0]]);
  });

  it("counts what's waiting on YOU, across every group — 0 is none", () => {
    expect(waitingCount(DATA)).toBe(2);
    expect(waitingCount(null)).toBe(0);
    expect(waitingCount(undefined)).toBe(0);
    expect(waitingCount({ ...DATA, groups: { ...DATA.groups, waiting: [] } })).toBe(0);
  });

  it("keeps a group's summary on its own tab", () => {
    expect(viewFor(DATA, "r0", rowOf).summaries.map((s) => s.name)).toEqual(["Auth cleanup"]);
    expect(viewFor(DATA, "r1", rowOf).summaries).toEqual([]);
    expect(viewFor(DATA, "all", rowOf).summaries).toHaveLength(1);
  });
});

describe("Outbox rows: say it before it happens", () => {
  it("names the approve button for the step it will take", () => {
    expect(shipVerb("commit")).toBe("Commit");
    expect(shipVerb(undefined)).toBe("Commit");
    expect(shipVerb("make_pr")).toBe("Open the PR");
    expect(shipVerb("push")).toBe("Push");
  });

  it("says what happens after it, from the session's fast-track", () => {
    expect(thenText("commit", "commit")).toBe("stays local — its fast-track ends at commit");
    expect(thenText("commit", "pr")).toBe("then push, PR");
    expect(thenText("make_pr", "merge")).toBe("then merge");
    expect(thenText("pr", "pr")).toBe("its fast-track ends at PR");
  });

  it("chips a waiting row by kind", () => {
    expect(waitingChip(DATA.groups.waiting[0])).toEqual({ text: "its agent is asking", cls: "warn" });
    expect(waitingChip(DATA.groups.waiting[1]).text).toBe("ready to commit — you asked to see it first");
    expect(waitingChip({ title: "x", kind: "approve", step: "pr" }).text).toBe(
      "ready to open the PR — you asked to see it first"
    );
    expect(waitingChip({ title: "x", kind: "ship_halted", reason: "hooks failed twice at mypy" }).cls).toBe("bad");
    expect(statText(DATA.groups.waiting[1].preview)).toBe("5 files +210 −32");
  });

  it("chips a shipped row with its PR and checks", () => {
    expect(shippedChip(DATA.groups.shipped[0])).toEqual({ text: "PR #318 · checks ✓", cls: "ok" });
    expect(shippedChip({ title: "x", pr_url: "https://g/pull/9", pr_state: "merged", checks: "pass" }).text).toBe(
      "PR #9 merged · checks ✓"
    );
    expect(shippedChip({ title: "x", pr_url: "https://g/pull/9", checks: "fail" }).cls).toBe("bad");
    expect(shippedChip({ title: "x" }).text).toBe("shipped");
    // Without a PR the chip says how far the lane went.
    expect(shippedChip({ title: "x", lane: "push" }).text).toBe("pushed");
    expect(shippedChip({ title: "x", lane: "commit" }).text).toBe("committed");
  });
});

describe("the approval card's message (review findings 23, 24)", () => {
  it("an edit belongs to ONE approval — the next one of the same session starts fresh", () => {
    const a = approvalKey({ title: "web", armed_at: 100, since: 150 });
    const b = approvalKey({ title: "web", armed_at: 900, since: 950 });
    expect(a).not.toBe(b);
    expect(approvalKey({ title: "web", armed_at: 100, since: 999 })).toBe(a);
  });

  it("the card shows the first line; the edit keeps the body", () => {
    expect(messageHead("feat: x\n\nwhy it matters")).toBe("feat: x …");
    expect(messageHead("feat: x")).toBe("feat: x");
    expect(messageHead(null)).toBe("");
  });
});

describe("the bell's one list: needsAttention", () => {
  const approve = DATA.groups.waiting[1];
  const prompt = DATA.groups.waiting[0];

  it("drops the Outbox's prompt item: the session's own answer row is that question", () => {
    const rows = needsAttention([{ p: 0, title: "jira-PAY-419", reason: "needs your answer" }], [prompt]);
    expect(rows.map((r) => [r.key, r.rank, !!r.attn, !!r.waiting])).toEqual([["jira-PAY-419", 0, true, false]]);
  });

  it("is one row per session: an approval outranks the session's other attention reasons", () => {
    const rows = needsAttention([{ p: 2, title: "web-dark-mode", reason: "checks failing" }], [approve]);
    expect(rows).toHaveLength(1);
    expect(rows[0].waiting?.kind).toBe("approve");
    expect(rows[0].rank).toBe(1);
  });

  it("keeps an answer row over an Outbox item for the same session", () => {
    const rows = needsAttention(
      [{ p: 0, title: "web-dark-mode", reason: "needs your answer" }],
      [{ ...approve, kind: "ship_halted" }]
    );
    expect(rows.map((r) => [r.key, r.rank, !!r.attn])).toEqual([["web-dark-mode", 0, true]]);
  });

  it("dedupes one branch's two windows to one row", () => {
    const rows = needsAttention([], [approve, { ...approve, title: "web-dark-mode-copy" }]);
    expect(rows.map((r) => r.title)).toEqual(["web-dark-mode"]);
  });

  it("keys a group-level item with no session as run:<id>", () => {
    const budget = { title: "", kind: "budget", run: { id: "r9", name: "Q4" }, actions: ["raise_budget", "stop"] };
    expect(needsAttention([], [budget]).map((r) => [r.key, r.title, r.rank])).toEqual([["run:r9", "", 1]]);
  });

  it("keeps every session-less item of one group: two unresolved ticket lines and its budget pause", () => {
    const r9 = { id: "r9", name: "Q4 batch" };
    const waiting = [
      { key: "run::r9::t1", title: "", kind: "failed", reason: "ticket PAY-1 not found", run: { ...r9, task: "t1" }, actions: ["retry", "skip"] },
      { key: "run::r9::t2", title: "", kind: "failed", reason: "ticket PAY-2 not found", run: { ...r9, task: "t2" }, actions: ["retry", "skip"] },
      { key: "run::r9", title: "", kind: "budget", run: r9, actions: ["raise_budget", "stop"] },
    ];
    const rows = needsAttention([], waiting);
    expect(rows.map((r) => r.key)).toEqual(["run::r9::t1", "run::r9::t2", "run::r9"]);
    expect(rows.map((r) => r.waiting?.kind)).toEqual(["failed", "failed", "budget"]);
    expect(rows.every((r) => r.waiting?.run?.id === "r9")).toBe(true);
  });

  it("orders answer → approvals/escalations → broken → checks failing → ready", () => {
    const rows = needsAttention(
      [
        { p: 0, title: "asking", reason: "needs your answer" },
        { p: 1, title: "broken", reason: "worktree setup failed" },
        { p: 2, title: "red", reason: "checks failing" },
        { p: 3, title: "ready", reason: "pushed — ready for PR" },
      ],
      [{ title: "stuck", kind: "stuck", run: { id: "r1", task: "t1" }, actions: ["retry", "skip"] }, approve]
    );
    expect(rows.map((r) => r.key)).toEqual(["asking", "stuck", "web-dark-mode", "broken", "red", "ready"]);
    expect(rows.map((r) => r.rank)).toEqual([0, 1, 1, 2, 3, 4]);
  });

  it("is empty with nothing on either side, and tolerates no Outbox at all", () => {
    expect(needsAttention([], undefined)).toEqual([]);
    expect(needsAttention([{ p: 3, title: "a", reason: "pushed — ready for PR" }], undefined)).toHaveLength(1);
  });
});
