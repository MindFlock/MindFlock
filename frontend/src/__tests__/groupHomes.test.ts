/** Where a group's own things live when it has no header on the rail.
 *
 * A split / one-for-all group is a family under its lead (lib/runs.ts never
 * gives it a header), so its finish toast and bell row land on the lead's
 * Thread tab (lib/showGroup.ts), and that Thread holds what a header's ⋯
 * would: a queued piece's Start now / Remove and a finished group's Copy
 * summary (RunLeadPanel). A header's Copy summary outlives the outbox's
 * week of summaries by reading the run's own record. */
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, it, expect, afterEach, vi } from "vitest";
import type { Instance, OutboxResponse, RunDTO } from "../api/types";
import { groupLanding, type RunInfo } from "../lib/runs";
import { summaryText } from "../components/outbox/outbox";
import { queryClient } from "../state/queries";

const opened: string[] = [];
const selected: string[] = [];
vi.mock("../lib/flockActions", () => ({ openThread: (t: string) => opened.push(t) }));
vi.mock("../lib/sessionActions", () => ({ selectSession: (t: string) => selected.push(t) }));

const { showGroup } = await import("../lib/showGroup");
const { RunLeadPanel } = await import("../components/grid/RunLeadPanel");

const g = globalThis as Record<string, unknown>;
const hadDoc = "document" in g;
const prevDoc = g.document;
const hadWin = "window" in g;
const prevWin = g.window;

afterEach(() => {
  opened.length = 0;
  selected.length = 0;
  queryClient.removeQueries({ queryKey: ["runs"] });
  queryClient.removeQueries({ queryKey: ["instances"] });
  if (hadDoc) g.document = prevDoc;
  else delete g.document;
  if (hadWin) g.window = prevWin;
  else delete g.window;
});

const row = (title: string, run?: { id: string; role: string }, extra: Partial<Instance> = {}) =>
  ({ title, run: run ? { name: "g", task: "", grouping: "together", ...run } : null, ...extra }) as unknown as Instance;

const splitRun = { id: "s1", name: "auth split", state: "done", split: true, lead: { title: "auth-lead" } } as unknown as RunInfo;

describe("groupLanding: a headerless group's toast / bell row", () => {
  const rows = [row("auth-lead", { id: "s1", role: "lead" }), row("auth-tokens", { id: "s1", role: "piece" }), row("solo")];

  it("goes to the lead's Thread while the lead is on the rail", () => {
    expect(groupLanding("s1", [splitRun], rows)).toEqual({ lead: "auth-lead" });
    // No cached runs (first fetch in flight): the row's own role says so.
    expect(groupLanding("s1", [], rows)).toEqual({ lead: "auth-lead" });
  });

  it("falls back to a member row when the lead was closed", () => {
    expect(groupLanding("s1", [splitRun], rows.slice(1))).toEqual({ row: "auth-tokens" });
  });

  it("is null when nothing of the group is left, and never picks a remote device's row", () => {
    expect(groupLanding("s1", [splitRun], [row("solo")])).toBeNull();
    expect(groupLanding("s1", [splitRun], [row("pi::auth-lead", { id: "s1", role: "lead" }, { device: "pi" })])).toBeNull();
    expect(groupLanding("", [splitRun], rows)).toBeNull();
    expect(groupLanding(null, null, null)).toBeNull();
  });
});

describe("showGroup: header first, then the lead, then nothing", () => {
  const noHeads = () => {
    g.document = { querySelectorAll: () => [] };
  };

  it("opens the lead's Thread for a split group (no header on the rail)", () => {
    noHeads();
    queryClient.setQueryData(["runs"], [splitRun]);
    queryClient.setQueryData(["instances"], [row("auth-lead", { id: "s1", role: "lead" })]);
    showGroup("s1");
    expect(opened).toEqual(["auth-lead"]);
    expect(selected).toEqual([]);
  });

  it("selects a member row when only a member is left", () => {
    noHeads();
    queryClient.setQueryData(["instances"], [row("b-2", { id: "e1", role: "task" })]);
    showGroup("e1");
    expect(selected).toEqual(["b-2"]);
  });

  it("does nothing — and throws nothing — for a group with nothing left", () => {
    noHeads();
    queryClient.setQueryData(["instances"], [row("solo")]);
    expect(() => showGroup("gone")).not.toThrow();
    expect(() => showGroup(null)).not.toThrow();
    expect(opened).toEqual([]);
    expect(selected).toEqual([]);
  });

  it("never falls back when the header is there", () => {
    const head = {
      dataset: { run: "e1" },
      offsetWidth: 0,
      scrollIntoView() {},
      classList: { add() {}, remove() {} },
    };
    g.document = { querySelectorAll: () => [head] };
    g.window = { setTimeout: () => 0 };
    queryClient.setQueryData(["instances"], [row("b-2", { id: "e1", role: "task" })]);
    showGroup("e1");
    expect(selected).toEqual([]);
  });
});

describe("summaryText: Copy summary for a group of any age", () => {
  const OUTBOX = { summaries: [{ run: "r0", name: "x", text_md: "## kept" }] } as unknown as OutboxResponse;

  it("prefers the outbox's copy, else the run's own record", () => {
    expect(summaryText(OUTBOX, "r0", { summary: { text_md: "## record" } })).toBe("## kept");
    // Finished 8+ days ago: /api/outbox dropped it, the run still has it.
    expect(summaryText(OUTBOX, "old", { summary: { text_md: "## record" } })).toBe("## record");
  });

  it("is empty when neither has one", () => {
    expect(summaryText(OUTBOX, "old", null)).toBe("");
    expect(summaryText(null, "old", { summary: { text_md: "   " } })).toBe("");
    expect(summaryText(undefined, "old", { summary: null })).toBe("");
  });
});

describe("RunLeadPanel: a headerless group's ⋯ items, on its lead's Thread", () => {
  const dto = (over: Partial<RunDTO>) =>
    ({
      id: "s1",
      name: "auth split",
      state: "running",
      paused: false,
      pause_reason: "",
      policy: { lane: "pr", ask_first: false, grouping: "together" },
      counts: { queued: 1, active: 1, needs_you: 0, shipped: 0, failed: 0, total: 2 },
      cost_usd: 0,
      created_at: 0,
      split: true,
      lead: { title: "auth-lead" },
      tasks: [
        { id: "t1", kind: "piece", text: "tokens", title: "auth-tokens", state: "working", reason: "" },
        { id: "t2", kind: "piece", text: "sessions", title: "auth-sessions", state: "queued", reason: "" },
      ],
      ...over,
    }) as RunDTO;
  const render = (run: RunDTO) =>
    renderToStaticMarkup(createElement(RunLeadPanel, { title: "auth-lead", me: undefined, run, rows: [] }));

  it("a queued piece has Start now and Remove; a working one has neither", () => {
    const html = render(dto({}));
    expect(html.match(/rb-start-now/g)?.length).toBe(1);
    expect(html.match(/rb-remove/g)?.length).toBe(1);
    expect(html).not.toContain("Copy summary");
  });

  it("a finished group offers Copy summary from its own record", () => {
    const done = render(dto({ state: "done", summary: { text_md: "## auth split\n- one PR" } }));
    expect(done).toContain("Copy summary");
    expect(done).not.toMatch(/rb-copy-summary"[^>]*disabled/);
    expect(done).not.toContain("rb-start-now");
    const none = render(dto({ state: "done", summary: null }));
    expect(none).toMatch(/<button[^>]*disabled=""[^>]*title="MindFlock has no summary for this group"/);
  });
});
