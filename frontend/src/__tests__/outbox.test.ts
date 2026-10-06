/** The Outbox as data (components/outbox/outbox.ts): one response, filtered
 * per tab, so a tab's badge is the length of exactly what the tab shows — and
 * one branch is one row however many windows sit on it. */
import { describe, it, expect } from "vitest";
import type { Instance, OutboxResponse } from "../api/types";
import {
  approvalKey,
  dedupe,
  messageHead,
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

  it("builds All · one tab per group · On their own, each counting its own rows", () => {
    const tabs = outboxTabs(DATA, rowOf);
    expect(tabs.map((t) => [t.key, t.label, t.count])).toEqual([
      ["all", "All", 7],
      ["r1", "Q4 payments", 5],
      ["own", "On their own", 2],
    ]);
    for (const t of tabs) expect(t.count).toBe(viewCount(viewFor(DATA, t.key, rowOf)));
  });

  it("has no On their own tab when there is no group, and nothing at all without data", () => {
    const solo: OutboxResponse = { ...DATA, groups: { waiting: [DATA.groups.waiting[1]], shipping: [], shipped: [], queued: [] } };
    expect(outboxTabs(solo, () => undefined).map((t) => t.key)).toEqual(["all"]);
    expect(outboxTabs(null, rowOf).map((t) => [t.key, t.count])).toEqual([["all", 0]]);
  });

  it("puts what's waiting on YOU on the top-bar badge — 0 hides it", () => {
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

  it("says what happens after it, from the session's lane", () => {
    expect(thenText("commit", "commit")).toBe("stays local — this session's lane ends at commit");
    expect(thenText("commit", "pr")).toBe("then push, PR");
    expect(thenText("make_pr", "merge")).toBe("then merge");
    expect(thenText("pr", "pr")).toBe("this session's lane ends at PR");
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
