/** MindFlock MCP families on the rail (SPEC "Rail"): the visual nesting, the
 * placement of a freshly spawned worker under its parent, and the wording of
 * the worker status line, the orchestrator's roll-up and its waiting / wrap-up
 * chip.
 *
 * The invariant that matters most is the one nesting must NOT touch: rail
 * order. Alt+N, Ctrl+Tab, the notification "[N]" and drag-and-drop all run
 * off the order the Sidebar renders and publishes as railOrder — nesting is
 * paint on top of it. The Sidebar block below renders the real component and
 * pins that the numbers and the row sequence are identical with and without
 * a family. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, beforeEach, vi } from "vitest";
import type { Instance } from "../api/types";
import {
  NEST_MAX,
  movedRailOrder,
  orderWithAfter,
  orderedKeys,
  placeNewWorkers,
  railNesting,
} from "../components/sidebar/ordering";
import {
  childrenByParent,
  currentReport,
  inFamily,
  parentChip,
  rollup,
  workerLine,
  workerOf,
  workerState,
  type FamilyRow,
} from "../lib/agentMessages";
import { Sidebar } from "../components/sidebar/Sidebar";
import { queryClient } from "../state/queries";
import { useUi } from "../state/store";

// Server-rendering the real Sidebar in the node environment: every external
// store reads its CLIENT snapshot — the rows' terminal / Map-seen stores have
// no server one (the mock), and zustand's would be its initial state rather
// than the order a test sets (zustand is a prebuilt dependency the mock can't
// reach, so renderRail copies the live state over the initial one). The
// instances poll also asks whether the page is hidden.
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

const rows = (spec: Array<[string, string?]>) => spec.map(([key, parent]) => ({ key, parent }));

describe("railNesting: visual only, adjacency only", () => {
  it("nests a parent's workers that directly follow it", () => {
    const n = railNesting(rows([["api"], ["b", "api"], ["s", "api"], ["u", "api"], ["web"]]));
    expect(n.map((x) => x.depth)).toEqual([0, 1, 1, 1, 0]);
    // The parent drops a connector into its first worker; each worker but the
    // last continues it (├), the last closes it (└).
    expect(n[0].stem).toBe(true);
    expect(n.map((x) => x.more)).toEqual([false, true, true, false, false]);
    expect(n[4]).toEqual({ depth: 0, more: false, guides: [], stem: false });
  });

  it("leaves a worker flat when something else sits between it and its family", () => {
    // A worker dragged below an unrelated session — or a window row, which
    // has no parent — is not drawn under its orchestrator.
    const n = railNesting(rows([["api"], ["b", "api"], ["web"], ["u", "api"]]));
    expect(n.map((x) => x.depth)).toEqual([0, 1, 0, 0]);
    expect(n[1].more).toBe(false);
    const w = railNesting(rows([["api"], ["\u0000assistant"], ["b", "api"]]));
    expect(w.map((x) => x.depth)).toEqual([0, 0, 0]);
    expect(w[0].stem).toBe(false);
  });

  it("never nests a worker that sits ABOVE its parent", () => {
    const n = railNesting(rows([["b", "api"], ["api"]]));
    expect(n.map((x) => x.depth)).toEqual([0, 0]);
  });

  it("keeps an ancestor's guide running past a nested subtree", () => {
    // api ─┬ w1 ── g1 (w1's own worker)
    //      └ w2
    const n = railNesting(rows([["api"], ["w1", "api"], ["g1", "w1"], ["w2", "api"]]));
    expect(n.map((x) => x.depth)).toEqual([0, 1, 2, 1]);
    expect(n[1]).toMatchObject({ more: true, stem: true });
    // g1 is the last at its level, but w1's line (level 1) passes through it
    // on the way down to w2.
    expect(n[2].more).toBe(false);
    expect(n[2].guides[1]).toBe(true);
    expect(n[3].more).toBe(false);
  });

  it("stops indenting past NEST_MAX", () => {
    const chain = rows([["a"], ["b", "a"], ["c", "b"], ["d", "c"], ["e", "d"]]);
    const n = railNesting(chain);
    expect(n.map((x) => x.depth)).toEqual([0, 1, 2, 3, 0]);
    expect(Math.max(...n.map((x) => x.depth))).toBe(NEST_MAX);
  });

  it("is a pure read: one entry per row, input untouched", () => {
    const input = rows([["api"], ["b", "api"], ["web"]]);
    const before = JSON.stringify(input);
    expect(railNesting(input)).toHaveLength(input.length);
    expect(JSON.stringify(input)).toBe(before);
    expect(railNesting([])).toEqual([]);
  });
});

describe("placeNewWorkers: a spawned worker files under its parent", () => {
  const live = (spec: Array<[string, string?]>) => spec.map(([title, parent]) => ({ title, parent }));

  it("slots a never-seen worker after its parent's family, not at the bottom", () => {
    const saved = ["web", "api", "w1", "other"];
    const next = placeNewWorkers(saved, live([["web"], ["api"], ["w1", "api"], ["other"], ["w2", "api"]]));
    expect(next).toEqual(["web", "api", "w1", "w2", "other"]);
    // Without placement the rail would have filed it last.
    expect(orderedKeys(["web", "api", "w1", "other", "w2"], saved)).toEqual(["web", "api", "w1", "other", "w2"]);
  });

  it("keeps workers created together in the order the server lists them", () => {
    const next = placeNewWorkers(["api", "web"], live([["api"], ["web"], ["a", "api"], ["b", "api"], ["c", "api"]]));
    expect(next).toEqual(["api", "a", "b", "c", "web"]);
  });

  it("places a grandchild under a parent that is itself new", () => {
    const next = placeNewWorkers(["api", "web"], live([["api"], ["web"], ["g", "w"], ["w", "api"]]));
    expect(next).toEqual(["api", "w", "g", "web"]);
  });

  it("never moves a worker the order already holds — a drag owns it", () => {
    // w1 was dragged below web on purpose.
    const dragged = movedRailOrder({
      saved: ["api", "w1", "web"],
      live: ["api", "w1", "web"],
      drag: "w1",
      target: "web",
      before: false,
    });
    expect(dragged).toEqual(["api", "web", "w1"]);
    const again = placeNewWorkers(dragged, live([["api"], ["w1", "api"], ["web"]]));
    expect(again).toBe(dragged);
  });

  it("returns the saved order itself when there is nothing to place", () => {
    const saved = ["api", "web"];
    expect(placeNewWorkers(saved, live([["api"], ["web"], ["solo"]]))).toBe(saved);
    // An orphan (its parent isn't live) has nowhere to go.
    expect(placeNewWorkers(saved, live([["api"], ["web"], ["o", "gone"]]))).toBe(saved);
  });

  it("merges rather than replaces: a sleeping row's slot survives", () => {
    const saved = ["api", "laptop::x", "web"];
    const next = placeNewWorkers(saved, live([["api"], ["web"], ["w", "api"]]));
    expect(next).toEqual(["api", "w", "laptop::x", "web"]);
  });

  it("is orderWithAfter underneath (the duplicate-window placement)", () => {
    expect(orderWithAfter(["api", "web", "w"], "w", "api")).toEqual(["api", "w", "web"]);
  });
});

// --- Wording -------------------------------------------------------------------

const NOW = 1_800_000_000;
const row = (title: string, extra: Partial<FamilyRow> = {}): FamilyRow => ({ title, parent: "api", ...extra });
const report = (status: string, ts = NOW - 240, summary = "14 tests pass") => ({ id: "m", status, summary, ts });
const id = (t: string) => t;

describe("workerLine (the worker's rail status line)", () => {
  it("maps activity + last_report to the five phrases", () => {
    const line = (r: FamilyRow) => workerLine(r, { nested: true, parentName: "api", now: NOW });
    expect(line(row("w", { activity: "idle", last_report: report("done") }))).toMatchObject({
      text: "✓ reported",
      cls: "rep-done",
      state: "done",
    });
    expect(line(row("w", { activity: "idle", last_report: report("blocked") })).text).toBe("✗ blocked");
    expect(line(row("w", { activity: "idle", last_report: report("failed") })).cls).toBe("rep-blocked");
    expect(line(row("w", { activity: "clarify" }))).toMatchObject({ text: "? needs your answer", cls: "rep-ask" });
    expect(line(row("w", { activity: "working", activity_since: NOW - 360 })).text).toBe("working · 6m");
    expect(line(row("w", { activity: "idle" }))).toMatchObject({ text: "idle — no report", cls: "rep-idle" });
  });

  it("names the parent when the row isn't drawn under it", () => {
    const r = row("w", { activity: "idle", last_report: report("done") });
    expect(workerLine(r, { nested: false, parentName: "api", now: NOW }).text).toBe("↳ api · ✓ reported");
    expect(workerLine(r, { nested: true, parentName: "api", now: NOW }).text).toBe("✓ reported");
  });

  it("puts the report summary in the tooltip", () => {
    const t = workerLine(row("w", { activity: "idle", last_report: report("done") }), {
      nested: true,
      parentName: "api",
      now: NOW,
    }).title;
    expect(t).toContain("Worker of “api”");
    expect(t).toContain("reported done 4m ago: 14 tests pass");
  });

  it("a prompt outranks a report; going back to work retires the report", () => {
    expect(workerState(row("w", { activity: "clarify", last_report: report("done") }), "clarify")).toBe("ask");
    // Its parent replied with more to do: busy SINCE the report → working.
    const back = row("w", { activity: "working", activity_since: NOW - 10, last_report: report("done", NOW - 60) });
    expect(currentReport(back, "working")).toBeNull();
    expect(workerState(back, "working")).toBe("working");
    // Working since BEFORE the report (the report came mid-turn) still counts.
    const mid = row("w", { activity: "working", activity_since: NOW - 120, last_report: report("done", NOW - 60) });
    expect(workerState(mid, "working")).toBe("done");
  });
});

describe("rollup (the orchestrator's sub-line), most urgent first", () => {
  const kids = (...acts: Array<[string, string?]>) =>
    acts.map(([activity, st], i) =>
      row("w" + i, { activity, activity_since: NOW - 60, last_report: st ? report(st, NOW - 30) : null })
    );
  const text = (k: FamilyRow[]) => rollup(k, id, undefined, NOW)!.parts.map((p) => p.text).join(" · ");

  it("reads the spec's four shapes", () => {
    expect(text(kids(["clarify"], ["idle", "done"], ["working"]))).toBe("1 needs you · 3 workers");
    expect(text(kids(["idle", "done"], ["idle", "done"], ["working"]))).toBe("2 of 3 reported");
    expect(text(kids(["idle", "done"], ["idle", "done"], ["idle", "done"]))).toBe("all 3 reported");
    expect(text(kids(["working"], ["working"], ["working"]))).toBe("3 working");
  });

  it("colours the urgent part gold and a full house green", () => {
    expect(rollup(kids(["clarify"], ["working"]), id, undefined, NOW)!.parts[0]).toEqual({
      text: "1 needs you",
      cls: "needs",
    });
    expect(rollup(kids(["idle", "done"]), id, undefined, NOW)!.parts.at(-1)!.cls).toBe("ok");
    expect(text(kids(["idle", "failed"], ["working"]))).toBe("1 failed · 1 of 2 reported");
  });

  it("names every worker in the tooltip and is null without workers", () => {
    const r = rollup(kids(["clarify"], ["idle", "done"]), id, undefined, NOW)!;
    expect(r.title).toContain("w0 — ? needs your answer");
    expect(r.title).toContain("w1 — ✓ reported");
    expect(r.title).toContain("Thread");
    expect(rollup([], id)).toBeNull();
  });
});

describe("parentChip: waiting / wrap up / normal", () => {
  const parent = (activity: string, status = "running") => ({ title: "api", activity, status });
  const done = row("a", { activity: "idle", last_report: report("done") });
  const busy = row("b", { activity: "working", activity_since: NOW - 60 });

  it("wrap up once every worker reported, while the parent is idle", () => {
    const c = parentChip(parent("idle"), [done, { ...done, title: "c" }], id)!;
    expect(c).toMatchObject({ kind: "wrap", label: "wrap up", cls: "wrapchip" });
    expect(c.title).toContain("you press Enter");
  });

  it("waiting while idle with a worker still out", () => {
    expect(parentChip(parent("idle"), [done, busy], id)).toMatchObject({
      kind: "waiting",
      label: "waiting",
      cls: "s-waiting",
    });
  });

  it("stays out of the way when the parent is busy, prompted or paused", () => {
    expect(parentChip(parent("working"), [done], id)).toBeNull();
    expect(parentChip(parent("clarify"), [done], id)).toBeNull();
    expect(parentChip(parent("idle", "paused"), [done], id)).toBeNull();
    expect(parentChip(parent("idle"), [], id)).toBeNull();
  });

  it("counts a blocked / failed report as reported (the wrap-up stops on it)", () => {
    const blocked = row("x", { activity: "idle", last_report: report("blocked") });
    expect(parentChip(parent("idle"), [done, blocked], id)!.kind).toBe("wrap");
  });
});

describe("childrenByParent / workerOf", () => {
  it("groups live local children under live parents only", () => {
    const m = childrenByParent([
      { title: "api" },
      { title: "w1", parent: "api" },
      { title: "w2", parent: "api" },
      { title: "orphan", parent: "gone" },
      { title: "self", parent: "self" },
      { title: "dev::r", parent: "api", device: "dev" },
    ]);
    expect([...m.keys()]).toEqual(["api"]);
    expect(m.get("api")!.map((r) => r.title)).toEqual(["w1", "w2"]);
  });

  it("phrases the bell's lineage suffix", () => {
    expect(workerOf("api", (t) => t.toUpperCase())).toBe("· worker of API");
    expect(workerOf(undefined, id)).toBe("");
  });
});

// --- The real Sidebar: nesting never renumbers -------------------------------------

const inst = (title: string, extra: Partial<Instance> = {}) =>
  ({ title, status: "running", activity: "idle", stage: "agent", branch: "", ...extra }) as Instance;

function renderRail(list: Instance[], devices: unknown[] = []) {
  Object.assign(useUi.getInitialState(), useUi.getState());
  queryClient.setQueryData<Instance[]>(["instances"], list);
  queryClient.setQueryData(["devices"], { devices, self: devices.length ? { device: "me", host: "me", os: "" } : null });
  const html = renderToStaticMarkup(
    createElement(QueryClientProvider, { client: queryClient }, createElement(Sidebar, { onOpenChat() {}, onOpenTodo() {} }))
  );
  // One entry per rail row, with that row's own markup (up to the next row).
  const parts = html.split(/(?=<li class="inst)/).slice(1);
  return parts.map((h) => {
    const m = h.match(/^<li class="(inst[^"]*)" data-title="([^"]+)"/)!;
    return { cls: m[1], title: m[2], idx: h.match(/<span class="idx"[^>]*>(\d*)<\/span>/)![1], html: h };
  });
}

describe("inFamily: who gets the one-click answer strip", () => {
  it("is a worker, a parent, or a session created with a playbook", () => {
    expect(inFamily({}, true, 0)).toBe(true);
    expect(inFamily({}, false, 2)).toBe(true);
    expect(inFamily({ playbook: "split" }, false, 0)).toBe(true);
    expect(inFamily({ playbook: "" }, false, 0)).toBe(false);
    expect(inFamily({}, false, 0)).toBe(false);
  });
});

describe("Sidebar: family nesting leaves rail order and numbering alone", () => {
  beforeEach(() => {
    useUi.setState({ order: ["web", "api", "w1", "other", "w2"], filter: "", hidden: new Set() });
  });

  it("numbers the same rows in the same sequence with and without a family", () => {
    const family = [
      inst("web"),
      inst("api", { activity: "working" }),
      inst("w1", { parent: "api", spawned: true, last_report: { id: "m", status: "done", summary: "", ts: 1 } }),
      inst("other"),
      inst("w2", { parent: "api", spawned: true, activity: "working" }),
    ];
    const flat = family.map((i) => ({ ...i, parent: "", spawned: false }));
    const nested = renderRail(family);
    const plain = renderRail(flat);
    expect(nested.map((r) => [r.title, r.idx])).toEqual(plain.map((r) => [r.title, r.idx]));
    expect(nested.map((r) => r.idx)).toEqual(["1", "2", "3", "4", "5"]);
    // The saved order drives the sequence; w2 sits under "other", so it is
    // not adjacent to its family and stays flat.
    expect(nested.map((r) => r.title)).toEqual(["web", "api", "w1", "other", "w2"]);
    expect(nested.find((r) => r.title === "w1")!.cls).toContain("nest-1");
    expect(nested.find((r) => r.title === "w2")!.cls).not.toContain("nest-");
    expect(plain.some((r) => r.cls.includes("nest-"))).toBe(false);
  });

  it("makes only a blocked FAMILY row focusable (keys 1–9) and swaps the parent's chip", () => {
    useUi.setState({ order: [] });
    const rep = { id: "m", status: "done", summary: "ok", ts: 1 };
    const list = [
      inst("api", { activity: "idle" }),
      inst("w1", { parent: "api", spawned: true, last_report: rep }),
      inst("w2", { parent: "api", spawned: true, activity: "clarify" }),
      inst("loner", { activity: "clarify" }),
    ];
    const r = renderRail(list);
    const by = (t: string) => r.find((x) => x.title === t)!.html;
    expect(by("w2")).toContain('tabindex="0"');
    // A session outside any family keeps today's row: no strip, no focus stop.
    expect(by("loner")).not.toContain("tabindex");
    expect(by("w2")).toContain("? needs your answer");
    expect(by("w1")).toContain("✓ reported");
    // api idle with a worker out → the dashed "waiting" chip, and the roll-up.
    expect(by("api")).toContain('class="stagechip s-waiting"');
    expect(by("api")).toContain("1 needs you");
    const all = renderRail(list.map((i) => (i.title === "w2" ? { ...i, activity: "idle", last_report: rep } : i)));
    const api = all.find((x) => x.title === "api")!.html;
    expect(api).toMatch(/<button type="button" class="stagechip wrapchip"[^>]*>wrap up<\/button>/);
    expect(api).toContain("all 2 reported");
  });

  it("gives a split orchestrator its answer strip before its first worker exists", () => {
    // E2E defect B: the first spawn_session prompt (and a whoami before it)
    // comes before any child — the strip was gated on "has children".
    useUi.setState({ order: [] });
    const list = [
      inst("orch", { activity: "clarify", playbook: "split" }),
      inst("plain", { activity: "clarify" }),
    ];
    const r = renderRail(list);
    const by = (t: string) => r.find((x) => x.title === t)!.html;
    expect(by("orch")).toContain('tabindex="0"');
    expect(by("plain")).not.toContain("tabindex");
  });

  // Regression: the Mac showed the WSL box's workers as top-level rows. Its
  // rail reads that box's sessions through remote control, titled
  // "<device>::<title>", and a worker's parent arrived bare ("orch"), so no
  // row matched it — and placement only ever ran over local rows.
  it("nests another device's worker under its parent and names it", () => {
    useUi.setState({ order: [] });
    const dev = { device: "wsl", host: "wsl", os: "linux", reachable: true, remote_control: true, connected: true };
    const remoteRow = (title: string, extra: Partial<Instance> = {}) =>
      inst("wsl::" + title, { device: "wsl", device_label: "wsl", display_title: title, ...extra } as Partial<Instance>);
    for (const parent of ["wsl::orch", "orch"]) {
      // Namespaced by the server, or bare from an older one: same rail.
      const list = [
        inst("orch"), // a LOCAL session of the same name must not adopt it
        remoteRow("orch"),
        remoteRow("other"),
        remoteRow("w1", { parent, spawned: true }),
      ];
      const r = renderRail(list, [dev]);
      const remote = r.filter((x) => x.title.startsWith("wsl::"));
      expect(remote.map((x) => x.title)).toEqual(["wsl::orch", "wsl::w1", "wsl::other"]);
      const w1 = remote[1];
      expect(w1.cls).toContain("nest-1");
      expect(w1.html).toContain("↳ orch");
      expect(r.find((x) => x.title === "orch")!.cls).not.toContain("nest-");
    }
  });

  it("renders a never-seen worker under its parent on first paint", () => {
    useUi.setState({ order: ["web", "api", "other"] });
    const list = [inst("web"), inst("api"), inst("other"), inst("w9", { parent: "api", spawned: true })];
    const r = renderRail(list);
    expect(r.map((x) => x.title)).toEqual(["web", "api", "w9", "other"]);
    expect(r.map((x) => x.idx)).toEqual(["1", "2", "3", "4"]);
    expect(r[2].cls).toContain("nest-1");
  });
});
