/** Ship lanes on the rail: run groups (lib/runs.ts), the header arithmetic,
 * the queued lines, the placement of a group's new members, run event wording
 * — and, against the REAL Sidebar, the rail-unification contract: a header or
 * a queued line is never a rail key, never numbered, and folding a group drops
 * its rows from the numbering exactly like a folded device. */
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, beforeEach, vi } from "vitest";
import type { Instance, RunTask } from "../api/types";
import { groupTitle, queuedLine, queuedTitle, runNote, shippedBadge, splitKeys, splitRail, type RunInfo } from "../lib/runs";
import { placeNewRunMembers } from "../components/sidebar/ordering";
import { Sidebar } from "../components/sidebar/Sidebar";
import { queryClient } from "../state/queries";
import { ruleOn } from "../state/runs";
import { useUi } from "../state/store";
import { notifFromEvent } from "../components/NotificationsBell";

// Same SSR shims as flockRail.test.ts: external stores read their client
// snapshot, and the instances poll asks whether the page is hidden.
vi.mock("react", async (orig) => {
  const R = (await orig()) as typeof import("react");
  return {
    ...R,
    useSyncExternalStore: <T,>(sub: (cb: () => void) => () => void, get: () => T) =>
      R.useSyncExternalStore(sub, get, get),
  };
});
const g = globalThis as Record<string, unknown>;
if (typeof g.document === "undefined") g.document = { hidden: false };

const RUN = { id: "r1", name: "Q4 payments", role: "task", grouping: "each" };
const inst = (title: string, extra: Partial<Instance> = {}) =>
  ({ title, status: "running", activity: "idle", stage: "agent", branch: "", ...extra }) as Instance;
const member = (title: string, task: string, extra: Partial<Instance> = {}) =>
  inst(title, { run: { ...RUN, task }, lane: { target: "pr", ask_first: false }, ...extra });
const task = (id: string, state: string, extra: Partial<RunTask> = {}): RunTask => ({
  id,
  kind: "task",
  text: "line " + id,
  title: "",
  state,
  reason: "",
  ...extra,
});
const runInfo = (extra: Partial<RunInfo> = {}): RunInfo => ({
  id: "r1",
  name: "Q4 payments",
  state: "running",
  paused: false,
  pause_reason: "",
  policy: { lane: "pr", ask_first: false, grouping: "each" },
  counts: { queued: 2, active: 3, needs_you: 1, shipped: 1, failed: 0, total: 6 },
  cost_usd: 0,
  created_at: 1,
  tasks: [
    task("t1", "shipped", { title: "a" }),
    task("t2", "shipping", { title: "b" }),
    task("t3", "working", { title: "c" }),
    task("t4", "needs_you", { title: "d", reason: "prompt" }),
    task("t5", "queued", { ticket_id: "PAY-421", text: "Stripe 2026-09" }),
    task("t6", "queued", { text: "Backfill refund ledger" }),
  ],
  ...extra,
});
const entries = (list: Instance[]) => list.map((i) => ({ key: i.title, inst: i }));
const SIX = () => [
  member("a", "t1", { stage: "pr" }),
  inst("solo", { lane: { target: "commit", ask_first: true } }),
  member("b", "t2"),
  member("c", "t3", { activity: "working" }),
  member("d", "t4", { activity: "clarify" }),
];

describe("splitRail: groups are headers over rows already on the rail", () => {
  it("pulls a group's members under it, in rail order, and leaves the rest on their own", () => {
    const s = splitRail(entries(SIX()), [runInfo()]);
    expect(s.groups).toHaveLength(1);
    const grp = s.groups[0];
    expect(grp.entries.map((e) => e.key)).toEqual(["a", "b", "c", "d"]);
    expect(s.own.map((e) => e.key)).toEqual(["solo"]);
    expect(grp.queued.map((t) => t.id)).toEqual(["t5", "t6"]);
    expect(grp.lane).toBe("→ PR");
    expect(grp.total).toBe(6);
    expect(shippedBadge(grp)).toBe("1/6 shipped");
    // needs you: a member on a prompt, deduped against its needs_you task.
    expect(grp.needs).toBe(1);
  });

  it("a cancelled group is finished but never reads as a success", () => {
    const s = splitRail(entries(SIX()), [runInfo({ state: "cancelled" })]);
    const grp = s.groups[0];
    expect(grp.done && grp.cancelled).toBe(true);
    expect(groupTitle(grp)).toContain("cancelled");
    expect(splitRail(entries(SIX()), [runInfo({ state: "done" })]).groups[0].cancelled).toBe(false);
  });

  it("never makes a header for a group of one, and never for a session on its own", () => {
    const one = runInfo({ counts: { queued: 0, active: 1, needs_you: 0, shipped: 0, failed: 0, total: 1 }, tasks: [task("t1", "working", { title: "a" })] });
    const s = splitRail(entries([member("a", "t1"), inst("solo")]), [one]);
    expect(s.groups).toEqual([]);
    expect(s.own.map((e) => e.key)).toEqual(["a", "solo"]);
    expect(splitRail(entries([inst("x"), inst("y")]), []).groups).toEqual([]);
  });

  it("still groups by the rows' own `run` when the runs route is missing", () => {
    const s = splitRail(entries([member("a", "t1"), member("b", "t2"), inst("solo")]), []);
    expect(s.groups.map((x) => [x.name, x.entries.length])).toEqual([["Q4 payments", 2]]);
    expect(s.groups[0].queued).toEqual([]);
  });

  it("keeps a group whose lines are all still queued, and drops a finished group's queue", () => {
    const fresh = splitRail(entries([inst("solo")]), [runInfo({ tasks: [task("t1", "queued"), task("t2", "queued")] })]);
    expect(fresh.groups[0].entries).toEqual([]);
    expect(fresh.groups[0].queued).toHaveLength(2);
    const done = splitRail(entries([member("a", "t1"), member("b", "t2")]), [
      runInfo({ state: "done_with_failures", counts: { queued: 0, active: 0, needs_you: 0, shipped: 5, failed: 1, total: 6 } }),
    ]);
    expect(done.groups[0].done).toBe(true);
    expect(done.groups[0].queued).toEqual([]);
    expect(shippedBadge(done.groups[0]) + " · " + done.groups[0].failed).toBe("5/6 shipped · 1");
  });

  it("leaves window rows and remote-device rows out of every group", () => {
    const rows = [
      { key: "\u0000assistant-chat" },
      { key: "a", inst: member("a", "t1") },
      { key: "dev::b", inst: { ...member("dev::b", "t2"), device: "dev" } },
      { key: "c", inst: member("c", "t3") },
    ];
    const s = splitRail(rows, [runInfo()]);
    expect(s.groups[0].entries.map((e) => e.key)).toEqual(["a", "c"]);
    expect(s.own.map((e) => e.key)).toEqual(["\u0000assistant-chat", "dev::b"]);
  });
});

describe("splitKeys: what the rail publishes as railOrder", () => {
  it("is the rendered row sequence — never a header, never a queued line", () => {
    const s = splitRail(entries(SIX()), [runInfo()]);
    const keys = splitKeys(s);
    expect(keys).toEqual(["a", "b", "c", "d", "solo"]);
    for (const t of s.groups[0].queued) {
      expect(keys).not.toContain(t.id);
      expect(keys).not.toContain(t.title);
    }
  });

  it("drops a folded group's rows, the way a folded device section does", () => {
    const s = splitRail(entries(SIX()), [runInfo()], { collapsed: new Set(["r1"]) });
    expect(s.groups[0].collapsed).toBe(true);
    expect(splitKeys(s)).toEqual(["solo"]);
  });
});

describe("queued lines", () => {
  it("say where they are in the queue", () => {
    expect([0, 1, 2, 3, 10, 11, 12].map(queuedLine)).toEqual([
      "queued · next free slot",
      "queued · 2nd",
      "queued · 3rd",
      "queued · 4th",
      "queued · 11th",
      "queued · 12th",
      "queued · 13th",
    ]);
  });

  it("are titled by ticket ref + text, or just the text", () => {
    expect(queuedTitle({ ticket_id: "PAY-421", text: "Stripe 2026-09", title: "" })).toBe("PAY-421 Stripe 2026-09");
    expect(queuedTitle({ text: "Backfill refund ledger", title: "" })).toBe("Backfill refund ledger");
  });
});

describe("placeNewRunMembers: a group's new session files after its group", () => {
  const live = (spec: Array<[string, string?]>) => spec.map(([title, run]) => ({ title, run: run ? { id: run } : null }));

  it("slots a never-seen member after the last of its group, not at the bottom", () => {
    const next = placeNewRunMembers(["a", "b", "solo"], live([["a", "r1"], ["b", "r1"], ["solo"], ["c", "r1"]]));
    expect(next).toEqual(["a", "b", "c", "solo"]);
  });

  it("keeps members created together in server order, and never moves a placed one", () => {
    expect(placeNewRunMembers(["a", "solo"], live([["a", "r1"], ["solo"], ["c", "r1"], ["d", "r1"]]))).toEqual([
      "a",
      "c",
      "d",
      "solo",
    ]);
    const saved = ["solo", "a"];
    expect(placeNewRunMembers(saved, live([["a", "r1"], ["solo"]]))).toBe(saved);
  });

  it("only ever writes session titles into the saved order", () => {
    const next = placeNewRunMembers([], live([["a", "r1"], ["b", "r1"]]));
    expect(next).toEqual(["a", "b"]);
    expect(next.some((k) => k.startsWith("\u0000") || k.startsWith("run:"))).toBe(false);
  });
});

describe("runNote: run events, phrased once", () => {
  const info = { name: (id: string) => (id === "r1" ? "Q4 payments" : ""), lane: () => "pr" };

  it("phrases an escalation and keys it by run, task, reason and incarnation", () => {
    const n = runNote("run.needs_you", { run: "r1", task: "t5", title: "jira-PAY-421", ref: "PAY-421", reason: "ship_halted", incarnation: 7 }, info)!;
    expect(n.text).toBe("Q4 payments: PAY-421 hooks failed twice");
    expect(n.cls).toBe("n-warn");
    expect(n.rule).toBe("run_needs_you");
    expect(n.dedupe).toBe("needs:r1:t5:ship_halted:7");
    expect(n.run).toBe("r1");
  });

  it("a group's second ask (plan round 2, a new release head, a later budget pause) is a new bell row", () => {
    const plan = (key: string) =>
      runNote("run.needs_you", { run: "r1", reason: "plan", title: "lead", incarnation: 0, key }, info)!.dedupe;
    expect(plan("r1:plan:1")).not.toBe(plan("r1:plan:2"));
    const budget = (key: string) =>
      runNote("run.needs_you", { run: "r1", task: "", reason: "budget", incarnation: 0, key }, info)!.dedupe;
    expect(budget("r1:budget:100")).not.toBe(budget("r1:budget:900"));
    // An older server sends no key: the old keying still applies.
    expect(runNote("run.needs_you", { run: "r1", reason: "plan", incarnation: 0 }, info)!.dedupe).toBe(
      "needs:r1:plan:0"
    );
  });

  it("never re-announces a prompt — the session's own clarify row already did", () => {
    expect(runNote("run.needs_you", { run: "r1", task: "t4", reason: "prompt" }, info)).toBeNull();
    expect(runNote("run.changed", { run: "r1" }, info)).toBeNull();
  });

  it("phrases a finished group in PRs for a PR lane", () => {
    const n = runNote("run.finished", { run: "r1", name: "Q4 payments", shipped: 5, failed: 1 }, info)!;
    expect(n.text).toBe("Q4 payments finished — 5 PRs, 1 failed");
    expect(n.rule).toBe("run_finished");
    expect(runNote("run.finished", { run: "r1", shipped: 2, failed: 0 }, { ...info, lane: () => "commit" })!.text).toBe(
      "Q4 payments finished — 2 shipped"
    );
  });

  it("a one-for-all group says what it shipped — one PR, never 'N PRs'", () => {
    const n = runNote(
      "run.finished",
      { run: "r1", name: "Notes", shipped: 3, failed: 0, outcome: "its branch was pushed; the PR was not opened — open it from the branch" },
      info
    )!;
    expect(n.text).toBe("Notes finished — its branch was pushed; the PR was not opened — open it from the branch");
    expect(n.text).not.toMatch(/3 PRs/);
  });

  it("keeps a shipped line bell-only (no rule gate)", () => {
    const n = runNote("run.task_shipped", { run: "r1", task: "t1", title: "jira-PAY-412", pr_url: "https://x/pull/318" }, info)!;
    expect(n.text).toBe("Q4 payments: jira-PAY-412 shipped — PR #318");
    expect(n.rule).toBe("");
  });
});

describe("the bell: run rows carry their group and their rule", () => {
  const env = (event: string, data: Record<string, unknown>) =>
    ({ seq: 1, event, session: "", old: null, new: null, ts: 1, data }) as never;

  it("names the group from the cached runs, and points at it", () => {
    queryClient.setQueryData(["runs"], [runInfo()]);
    const n = notifFromEvent(env("run.needs_you", { run: "r1", task: "t5", ref: "PAY-421", reason: "stuck" }))!;
    expect(n.text).toBe("Q4 payments: PAY-421 stalled twice — no diff, no report");
    expect(n.run).toBe("r1");
    expect(n.rule).toBe("run_needs_you");
    expect(notifFromEvent(env("run.finished", { run: "r1", shipped: 6, failed: 0 }))!.text).toBe(
      "Q4 payments finished — 6 PRs"
    );
  });

  it("stays out of the feed for a refetch-only event and a prompt", () => {
    expect(notifFromEvent(env("run.changed", { run: "r1" }))).toBeNull();
    expect(notifFromEvent(env("run.needs_you", { run: "r1", reason: "prompt" }))).toBeNull();
  });

  it("gates on the rule switch; a rule the server doesn't list counts as on", () => {
    queryClient.setQueryData(["notify-config"], { rules: [{ id: "run_needs_you", enabled: false }] });
    expect(ruleOn("run_needs_you")).toBe(false);
    expect(ruleOn("run_finished")).toBe(true);
    expect(ruleOn("")).toBe(true);
    queryClient.setQueryData(["notify-config"], { rules: [] });
    expect(ruleOn("run_needs_you")).toBe(true);
  });
});

// --- The real Sidebar ----------------------------------------------------------

function renderRail(list: Instance[], runs: RunInfo[]) {
  Object.assign(useUi.getInitialState(), useUi.getState());
  queryClient.setQueryData<Instance[]>(["instances"], list);
  queryClient.setQueryData(["devices"], { devices: [], self: null });
  queryClient.setQueryData(["runs"], runs);
  const html = renderToStaticMarkup(
    createElement(QueryClientProvider, { client: queryClient }, createElement(Sidebar, { onOpenChat() {}, onOpenTodo() {} }))
  );
  const parts = html.split(/(?=<li class=")/).slice(1);
  return parts.map((h) => {
    const cls = h.match(/^<li class="([^"]*)"/)![1];
    return {
      cls,
      title: h.match(/data-title="([^"]+)"/)?.[1] || "",
      idx: h.match(/<span class="idx"[^>]*>(\d*)<\/span>/)?.[1] ?? null,
      html: h,
    };
  });
}

describe("Sidebar: run groups never renumber the rail", () => {
  beforeEach(() => {
    useUi.setState({ order: ["a", "solo", "b", "c", "d"], filter: "", hidden: new Set(), collapsedRuns: new Set() });
  });

  it("numbers rows 1..N in the rendered sequence — the sequence railOrder publishes", () => {
    const list = SIX();
    const r = renderRail(list, [runInfo()]);
    const rows = r.filter((x) => x.cls.startsWith("inst") && !x.cls.includes("run-queued"));
    expect(rows.map((x) => x.title)).toEqual(["a", "b", "c", "d", "solo"]);
    expect(rows.map((x) => x.idx)).toEqual(["1", "2", "3", "4", "5"]);
    // Alt+N reads railOrder[N-1]; Sidebar publishes splitKeys of the same split.
    const keys = splitKeys(splitRail(entries(list), [runInfo()], { collapsed: new Set() }));
    expect(keys).toEqual(rows.map((x) => x.title));
    // The saved order is untouched by grouping: no header ever lands in it.
    expect(useUi.getState().order).toEqual(["a", "solo", "b", "c", "d"]);
  });

  it("renders the header, then its rows, its queued lines, then On their own", () => {
    const r = renderRail(SIX(), [runInfo()]);
    const kinds = r.map((x) =>
      x.cls.includes("run-own") ? "own" : x.cls.includes("run-group-head") ? "head" : x.cls.includes("run-queued") ? "queued" : x.title
    );
    expect(kinds).toEqual(["head", "a", "b", "c", "d", "queued", "queued", "own", "solo"]);
    const head = r[0].html;
    expect(head).toContain('<span class="dev-name">Q4 payments</span>');
    expect(head).toContain('<span class="rg-lane">→ PR</span>');
    expect(head).toContain(">1/6 shipped<");
    expect(head).toMatch(/class="dev-badge rg-needs"[^>]*>1</);
  });

  it("gives a queued line no number, no drag handle and no ✕", () => {
    const q = renderRail(SIX(), [runInfo()]).filter((x) => x.cls.includes("run-queued"));
    expect(q).toHaveLength(2);
    for (const x of q) {
      expect(x.idx).toBe("");
      expect(x.html).not.toContain("draggable");
      expect(x.html).not.toContain('class="kill');
      expect(x.html).not.toContain("data-title");
    }
    expect(q[0].html).toContain("PAY-421 Stripe 2026-09");
    expect(q[0].html).toContain("queued · next free slot");
    expect(q[1].html).toContain("queued · 2nd");
  });

  it("folds: the group's rows and queued lines go, and numbering closes up", () => {
    useUi.setState({ collapsedRuns: new Set(["r1"]) });
    const r = renderRail(SIX(), [runInfo()]);
    expect(r[0].cls).toContain("is-folded");
    expect(r[0].html).toContain("▸");
    const rows = r.filter((x) => x.cls.startsWith("inst"));
    expect(rows.map((x) => [x.title, x.idx])).toEqual([["solo", "1"]]);
  });

  it("shows On their own only under a group, and a flat rail with no groups", () => {
    const flat = renderRail([inst("x"), inst("y")], []);
    expect(flat.some((x) => x.cls.includes("run-group-head"))).toBe(false);
    expect(flat.map((x) => x.idx)).toEqual(["1", "2"]);
  });

  it("leads every lane row with its lane, and gives a non-family member the answer strip", () => {
    const r = renderRail(SIX(), [runInfo()]);
    const by = (t: string) => r.find((x) => x.title === t)!.html;
    expect(by("c")).toContain('<span class="sl-lead">→ PR</span>');
    expect(by("solo")).toContain('<span class="sl-lead">→ commit, asks first</span>');
    expect(by("d")).toContain('<span class="sl-lead">? needs your answer</span>');
    // d is in no family, but it is in a group: its prompt is answerable in place
    // (the strip's dialog loads client-side; the focus stop is the SSR tell).
    expect(by("d")).toContain('tabindex="0"');
    // A session in no group and with no lane keeps today's row.
    const loner = renderRail([inst("loner", { activity: "clarify" }), inst("other")], []);
    expect(loner[0].html).not.toContain("tabindex");
  });
});
