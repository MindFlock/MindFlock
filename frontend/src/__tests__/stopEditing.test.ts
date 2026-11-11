/** Editing the stops on a line, n8n style (docs/flights-stop-editing.md): the
 * pure rules in lib/stopEditing (where a dropped stop chip lands, ⇤ / ⇥, what
 * Undo tacks back, each box's right-click menu), the move route, and the
 * canvas's markup — the hover toolbar on a stop that can change and on no
 * other, the level of detail, the end + under a stop drag, the menu. */

/// <reference types="vite/client" />
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { Flight, FlightStep, StepKindDef } from "../lib/flights";
import type { DiagramNode, Placed } from "../lib/flightDiagram";
import type { StopMenuCtx } from "../lib/stopEditing";

const api = vi.fn(async (..._args: unknown[]) => ({}));
vi.mock("../api/client", () => ({ api: (...a: unknown[]) => api(...a), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/errorPop", () => ({ errorPop: vi.fn() }));

const { FlywayCanvas } = await import("../components/flights/FlywayCanvas");
const { StopMenu } = await import("../components/flights/StopMenu");
const { flightDiagram, flywayDiagram, layoutDiagram } = await import("../lib/flightDiagram");
const { STOP_MIME, toggleHalt } = await import("../lib/flights");
const { moveStop, setHalt } = await import("../lib/flightsApi");
const { canShift, dropGapFor, haltMark, insertGaps, isStopDrag, menuItemsFor, undoPayload } = await import("../lib/stopEditing");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}): FlightStep => ({ id, kind, title, config, on_fail: "halt" });
const STEPS = [
  step("s1", "agent", "Work the ticket", { prompt: "Fix the login bug" }),
  step("s2", "ship", "Push", { lane: "push" }),
  step("s3", "you", "Approve the merge", { text: "Look at the PR first" }),
  step("s4", "ship", "Merge", { lane: "merge" }),
];
const KINDS: StepKindDef[] = [
  { kind: "agent", label: "Agent", actor: "robot", summary: "The agent works", fields: [{ name: "prompt", type: "textarea", label: "Prompt" }], default_title: "Agent works" },
  { kind: "ship", label: "Ship", actor: "robot", summary: "Commit, push, PR or merge", fields: [{ name: "lane", type: "select", label: "Lane", options: ["commit", "push", "pr", "merge"] }], default_title: "Ship" },
  { kind: "you", label: "Perch", actor: "you", summary: "Waits for you to press Done", fields: [{ name: "text", type: "textarea", label: "What to do" }], default_title: "Check it" },
];
const NOW_S = Math.floor(Date.now() / 1000);
function flight(over: Partial<Flight> = {}): Flight {
  return {
    id: "f_1",
    plan_id: "builtin:ticket",
    plan_name: "Ticket to merge",
    steps: STEPS,
    subject: { kind: "ticket", source: "sc", id: "sc-12", title: "Fix the login", url: "" },
    session: "sc-12-fix-login",
    repo_root: "/r",
    branch: "sc-12",
    state: "waiting_you",
    cursor: 1,
    step_states: {
      s1: { state: "done", since: NOW_S - 900, note: "", detail: {}, rounds: 0 },
      s2: { state: "waiting_you", since: NOW_S - 300, note: "", detail: {}, rounds: 0 },
    },
    reason: "",
    history: [],
    started: NOW_S - 900,
    updated: NOW_S - 300,
    finished: 0,
    by: "intake",
    ...over,
  };
}

const flyway = flywayDiagram(STEPS, KINDS);
const node = (over: Partial<DiagramNode>): DiagramNode => ({ id: "n", kind: "stop", row: "main", title: "Push", sub: "", state: "pending", icon: "ship", ...over });
const box = (n: DiagramNode, x: number, y = 0, w = 100, h = 60): Placed => ({ node: n, x, y, w, h });
const center = (b: Placed) => ({ x: b.x + b.w / 2, y: b.y + b.h / 2 });

describe("where a stop chip dropped on the line lands", () => {
  const l = layoutDiagram(flyway);
  const stops = l.boxes.filter((b) => b.node.kind === "stop");
  const add = l.boxes.find((b) => b.node.kind === "add")!;

  it("a + before every stop but the first, never after the trailing +", () => {
    expect(insertGaps(l.boxes, 0, false).map((g) => g.at)).toEqual([1, 2, 3]);
    // A live flight: only after its current stop.
    expect(insertGaps(l.boxes, 3, false).map((g) => g.at)).toEqual([3]);
  });

  it("the gap nearest the pointer, else the end", () => {
    const [g1, g2, g3] = insertGaps(l.boxes, 0, false);
    expect(dropGapFor(l.boxes, { x: g2.x + 4, y: 0 }, 0, false)).toBe(2);
    // Over the middle of a box: the nearer of its two gaps.
    expect(dropGapFor(l.boxes, { x: center(stops[1]).x - 10, y: 30 }, 0, false)).toBe(g1.at);
    // Past the last stop, on the trailing +: the end.
    expect(dropGapFor(l.boxes, center(add), 0, false)).toBeNull();
    expect(dropGapFor(l.boxes, { x: add.x + add.w + 200, y: 0 }, 0, false)).toBeNull();
    // Before the current stop it can't go: the first gap it may take.
    expect(dropGapFor(l.boxes, center(stops[0]), 3, false)).toBe(g3.at);
    // No gap to take at all: the end.
    expect(dropGapFor(l.boxes, center(stops[0]), 9, false)).toBeNull();
  });

  it("top to bottom on a phone", () => {
    const v = layoutDiagram(flyway, { vertical: true });
    const g = insertGaps(v.boxes, 0, true);
    expect(g.map((x) => x.at)).toEqual([1, 2, 3]);
    expect(dropGapFor(v.boxes, { x: 0, y: g[1].y }, 0, true)).toBe(2);
  });

  it("never across a column of branches: the join point is the next box", () => {
    const a = node({ id: "stop:a", index: 0 });
    const b = node({ id: "stop:b", index: 1 });
    const c = node({ id: "stop:c", index: 2 });
    const w = node({ id: "branch:w", kind: "branch", row: "branch", branch: { from: "stop:a", to: "stop:b", state: "working", inner: "" } });
    const boxes = [box(a, 0), box(w, 130, 80), box(b, 260), box(c, 390), box(node({ id: "add", kind: "add" }), 520)];
    expect(insertGaps(boxes, 0, false).map((g) => g.at)).toEqual([2]);
    expect(dropGapFor(boxes, { x: 240, y: 30 }, 0, false)).toBe(2);
  });

  it("knows a stop chip, and a saved stop, from anything else dragged", () => {
    expect(isStopDrag([STOP_MIME, "text/plain"])).toBe(true);
    // Round 11: a saved stop from the shelf (FLYWAY_MIME) tacks on as a group.
    expect(isStopDrag(["application/x-mindflock-flyway"])).toBe(true);
    expect(isStopDrag(["text/plain"])).toBe(false);
    expect(isStopDrag(null)).toBe(false);
  });
});

describe("moving a stop one place", () => {
  const d = flightDiagram(flight({ cursor: 1 }), KINDS);
  const stops = d.nodes.filter((n) => n.kind === "stop");

  it("only between two stops that can change, never before the current one", () => {
    // s1 done, s2 the perch, s3 + s4 pending.
    expect(canShift(stops, 2, -1, d.insertFrom)).toBe(false);
    expect(canShift(stops, 2, 1, d.insertFrom)).toBe(true);
    expect(canShift(stops, 3, -1, d.insertFrom)).toBe(true);
    expect(canShift(stops, 3, 1, d.insertFrom)).toBe(false);
    expect(canShift(stops, 1, 1, d.insertFrom)).toBe(false);
  });

  it("posts the move route with the index in the final list", async () => {
    api.mockClear();
    expect(await moveStop("f 1", "s3", 3)).toBe(true);
    expect(api).toHaveBeenCalledWith("/api/flights/f%201/steps/s3/move", { json: { at: 3 } });
  });
});

describe("Undo after a remove", () => {
  const s = STEPS[2];

  it("back at its old index while that is still ahead of the flight", () => {
    // s3 (index 2) removed from a flight at stop 1: three stops left.
    expect(undoPayload(s, 2, { cursor: 1, steps: STEPS.slice(0, 2).concat(STEPS[3]) })).toEqual({ step: s, at: 2 });
  });

  it("at the end once the flight has reached it, or it would be the last", () => {
    expect(undoPayload(s, 2, { cursor: 2, steps: STEPS.slice(0, 3) })).toEqual({ step: s });
    expect(undoPayload(s, 3, { cursor: 1, steps: STEPS.slice(0, 3) })).toEqual({ step: s });
    expect(undoPayload(s, 2, null)).toEqual({ step: s });
  });
});

describe("a box's right-click menu", () => {
  const all: StopMenuCtx = { can: { edit: true, move: true, duplicate: true, remove: true, open: true }, earlier: false, later: true, live: true };
  const keys = (n: DiagramNode, ctx = all) => menuItemsFor(n, ctx).map((i) => i.key + (i.disabled ? "-" : ""));

  it("a stop that can change: Edit, Earlier, Later, Duplicate, Remove", () => {
    expect(keys(node({ editable: true }))).toEqual(["edit", "earlier-", "later", "duplicate", "remove"]);
    expect(menuItemsFor(node({ editable: true }), all).find((i) => i.key === "remove")!.danger).toBe(true);
    // Only what the host can do.
    expect(keys(node({ editable: true }), { ...all, can: { remove: true } })).toEqual(["remove"]);
  });

  it("the flight's current stop: its own parts, its dialog (read-only), and that it can't change", () => {
    expect(keys(node({ state: "perched" }))).toEqual(["done", "skip", "edit", "note"]);
    expect(keys(node({ state: "halted" }))).toEqual(["retry", "fix", "skip", "edit", "note"]);
    expect(keys(node({ state: "current" }))).toEqual(["skip", "edit", "note"]);
    expect(menuItemsFor(node({ state: "current" }), all).find((i) => i.key === "edit")!.label).toBe("Details");
    expect(menuItemsFor(node({ state: "current" }), all).at(-1)!.label).toBe("The route is here — it can't change now");
    // Where the flight is: Remove halts it there.
    expect(menuItemsFor(node({ state: "current" }), { ...all, here: true }).find((i) => i.key === "remove")).toMatchObject({ label: "Remove — the route pauses here", danger: true });
    // No flight to act on: its dialog and the note.
    expect(keys(node({ state: "perched" }), { ...all, live: false })).toEqual(["edit", "note"]);
  });

  it("a stop that has flown says so, opens read-only and comes off the line", () => {
    for (const state of ["done", "skipped", "failed"] as const) {
      expect(keys(node({ state }))).toEqual(["edit", "remove", "note"]);
      expect(menuItemsFor(node({ state }), all).find((i) => i.key === "remove")!.label).toBe("Remove from the route");
      expect(menuItemsFor(node({ state }), all).at(-1)).toEqual(expect.objectContaining({ key: "note", label: "This stop has run", note: true }));
    }
    // Not on a line with no flight to take it off.
    expect(keys(node({ state: "done" }), { ...all, live: false })).toEqual(["edit", "note"]);
  });

  it("the trailing + adds a stop; a session box opens", () => {
    expect(menuItemsFor(node({ kind: "add", id: "add" }), all).map((i) => i.label)).toEqual(["Add a stop"]);
    for (const kind of ["session", "parent", "child", "branch"] as const) expect(keys(node({ kind, session: "w1" }))).toEqual(["open"]);
    expect(keys(node({ kind: "session" }), { ...all, can: {} })).toEqual([]);
  });

  it("draws as a menu of its own — buttons, the note muted, never a native one", () => {
    const html = renderToStaticMarkup(
      createElement(StopMenu, { x: 40, y: 12, label: "Push — what you can do", items: menuItemsFor(node({ state: "perched" }), all), onPick: () => {}, onClose: () => {} })
    );
    expect(html).toContain('role="menu"');
    expect(html).toContain('aria-label="Push — what you can do"');
    expect(html).toContain("left:40px;top:12px");
    expect(html.match(/<button[^>]*role="menuitem"/g)).toHaveLength(3);
    expect(html).toMatch(/class="fl-stopmenu-note" role="menuitem" aria-disabled="true"[^>]*>The route is here/);
    expect(html).not.toContain("<select");
  });
});

describe("the canvas", () => {
  const handlers = { onSelect: () => {}, onMove: () => {}, onRemove: () => {}, onDuplicate: () => {} };
  const render = (props: Record<string, unknown>) => renderToStaticMarkup(createElement(FlywayCanvas, { label: "Stops", vertical: false, ...props } as never));
  const toolbars = (html: string) => [...html.matchAll(/role="toolbar" aria-label="Change “([^”]+)”"/g)].map((m) => m[1]);

  it("a hover toolbar on every stop of a flyway being edited: Edit, Earlier, Later, Duplicate, Remove", () => {
    const html = render({ diagram: flyway, ...handlers });
    expect(toolbars(html)).toEqual(["Work the ticket", "Push", "Wait for me: Approve the merge", "Merge"]);
    for (const what of ["Edit “Push”", "Move “Push” earlier", "Move “Push” later", "Duplicate “Push”", "Remove “Push”"]) expect(html).toContain('aria-label="' + what + '"');
    // The ends can't move past the line.
    expect(html).toMatch(/aria-label="Move “Work the ticket” earlier" title="[^"]*" disabled=""/);
    expect(html).toMatch(/aria-label="Move “Merge” later" title="[^"]*" disabled=""/);
    expect(html).toContain('title="Remove this stop from the route (Delete)"');
  });

  it("in the air: only on the stops still to come — never on a done one or the current one", () => {
    const html = render({ diagram: flightDiagram(flight({ cursor: 1 }), KINDS), ...handlers });
    expect(toolbars(html)).toEqual(["Wait for me: Approve the merge", "Merge"]);
    expect(html).not.toContain("Change “Work the ticket”");
    expect(html).not.toContain("Change “Push”");
    // Approve can't trade places with the current stop.
    expect(html).toMatch(/aria-label="Move “Wait for me: Approve the merge” earlier" title="[^"]*" disabled=""/);
  });

  it("no toolbar without anything to change a stop with", () => {
    expect(toolbars(render({ diagram: flyway, onSelect: () => {} }))).toEqual([]);
  });

  it("level of detail: near draws the detail line whatever the size, far the head alone — the boxes keep their size", () => {
    const styles = (html: string) => [...html.matchAll(/data-node="[^"]+"[^>]*style="([^"]*)"/g)].map((m) => m[1]);
    const mid = render({ diagram: flyway, size: "compact" });
    const near = render({ diagram: flyway, size: "compact", lod: "near" });
    const far = render({ diagram: flyway, size: "wide", lod: "far" });
    expect(mid).toContain("fl-lod-mid");
    expect(near).toContain("fl-canvas fl-size-compact fl-lod-near");
    expect(near).toContain('<div class="fl-box-detail">Fix the login bug</div>');
    expect(near).toContain('<div class="fl-box-detail">Look at the PR first</div>');
    expect(near).toContain('class="fl-box-sub"');
    expect(mid).not.toContain("fl-box-detail");
    expect(styles(near)).toEqual(styles(mid));
    expect(far).toContain("fl-lod-far");
    expect(far).not.toContain("fl-box-detail");
    expect(far).not.toContain("fl-box-sub");
    expect(styles(far)).toEqual(styles(render({ diagram: flyway, size: "wide" })));
  });

  it("a stop chip dragged from the shelf: the end + says what it tacks on", () => {
    const html = render({ diagram: flyway, onDropStop: () => {}, dragStop: "Ship" });
    expect(html).toMatch(/class="fl-box fl-box-add fl-drop[^"]*"[^>]*aria-label="Add Ship"/);
    expect(html).toContain('<span class="fl-add-label">Add Ship</span>');
    expect(render({ diagram: flyway, onDropStop: () => {} })).not.toContain("Add Ship");
  });

  it("the gap + is counter-scaled by the Sky's zoom", () => {
    expect(render({ diagram: flyway, onInsert: () => {} })).toContain("scale(calc(1 / var(--sky-z, 1)))");
  });
});

describe("halt points", () => {
  const all: StopMenuCtx = { can: { edit: true, move: true, duplicate: true, remove: true, open: true, halt: true }, earlier: false, later: true, live: true };
  const keys = (n: DiagramNode, ctx = all) => menuItemsFor(n, ctx).map((i) => i.key + (i.disabled ? "-" : ""));
  const labels = (n: DiagramNode, ctx = all) => menuItemsFor(n, ctx).map((i) => i.label);
  const push = STEPS[1];
  const halting = (st: FlightStep) => ({ ...st, halt: true });

  it("a stop that can change: Add halt point, or Remove halt point on a halt point", () => {
    expect(keys(node({ editable: true, step: push }))).toEqual(["edit", "earlier-", "later", "duplicate", "halt", "remove"]);
    expect(labels(node({ editable: true, step: push }))).toContain("Add halt point");
    expect(labels(node({ editable: true, step: halting(push) }))).toContain("Remove halt point");
    expect(labels(node({ editable: true, step: halting(push) }))).not.toContain("Add halt point");
    for (const i of menuItemsFor(node({ editable: true, step: push }), all)) expect(i.title).toBeTruthy();
    // Only with a host that can mark it.
    expect(keys(node({ editable: true, step: push }), { ...all, can: { ...all.can, halt: false } })).not.toContain("halt");
  });

  it("the flight's current stop: Pause while it flies or waits on you, Carry on while held", () => {
    const here = { ...all, here: true };
    expect(keys(node({ state: "perched", step: push }), here)).toEqual(["pause", "done", "skip", "halt", "edit", "remove", "note"]);
    expect(keys(node({ state: "current", step: push }), here)).toEqual(["pause", "skip", "halt", "edit", "remove", "note"]);
    expect(labels(node({ state: "current", step: push }), here)[0]).toBe("Pause");
    expect(keys(node({ state: "paused", step: halting(push) }), here)).toEqual(["resume", "skip", "halt", "edit", "remove", "note"]);
    expect(labels(node({ state: "paused", step: halting(push) }), here)).toEqual([
      "Carry on",
      "Skip",
      "Remove halt point",
      "Details",
      "Remove — the route pauses here",
      "The route is here — it can't change now",
    ]);
    // Halted by a failure: Retry, not Carry on.
    expect(keys(node({ state: "halted", step: push }), here)).toEqual(["retry", "fix", "skip", "halt", "edit", "remove", "note"]);
    // Not the current stop: no Pause (as before), no Remove.
    expect(keys(node({ state: "perched", step: push }))).toEqual(["done", "skip", "edit", "note"]);
    // Never in the UI: "breakpoint", or the old "Halt now" / "Carry on past here".
    for (const st of ["current", "paused", "perched"] as const)
      for (const i of menuItemsFor(node({ state: st, step: push }), here)) expect(i.label + i.title).not.toMatch(/breakpoint|halt now|carry on past/i);
  });

  it("a stop that has flown can't be a halt point", () => {
    for (const state of ["done", "skipped", "failed"] as const) expect(keys(node({ state, step: push }), { ...all, here: true })).toEqual(["edit", "remove", "note"]);
  });

  it("the badge: on a halt point only, saying the flight stops there", () => {
    expect(haltMark(node({ step: push }))).toBeNull();
    expect(haltMark(node({ step: halting(push) }))).toEqual({ glyph: "⏸", title: expect.stringContaining("Halt point") });
    expect(haltMark(node({ state: "paused", step: halting(push) }))!.title).toContain("paused here");
    expect(haltMark(node({ kind: "session", step: halting(push) }))).toBeNull();
  });

  it("toggleHalt (the flyway editor's draft) sets and clears one stop's halt", () => {
    const on = toggleHalt(STEPS, 1);
    expect(on[1]).toEqual({ ...STEPS[1], halt: true });
    expect(on.filter((st) => st.halt)).toHaveLength(1);
    expect(STEPS[1].halt).toBeUndefined(); // a copy
    const off = toggleHalt(on, 1);
    expect(off[1]).toEqual(STEPS[1]);
    expect("halt" in off[1]).toBe(false);
    expect(toggleHalt(STEPS, 9)).toEqual(STEPS);
  });

  it("setHalt patches {halt} alone", async () => {
    api.mockClear();
    expect(await setHalt("f 1", "s3", true)).toBe(true);
    expect(api).toHaveBeenCalledWith("/api/flights/f%201/steps/s3", { method: "PATCH", json: { halt: true } });
    await setHalt("f_1", "s2", false);
    expect(api).toHaveBeenCalledWith("/api/flights/f_1/steps/s2", { method: "PATCH", json: { halt: false } });
  });

  describe("on the canvas", () => {
    const handlers = { onSelect: () => {}, onMove: () => {}, onRemove: () => {}, onDuplicate: () => {}, onToggleHalt: () => {} };
    const render = (props: Record<string, unknown>) => renderToStaticMarkup(createElement(FlywayCanvas, { label: "Stops", vertical: false, ...props } as never));
    const boxOf = (html: string, id: string) => {
      const from = html.indexOf('data-node="' + id + '"');
      const start = html.lastIndexOf("<div", from);
      const next = html.indexOf('role="listitem"', from + 1);
      return html.slice(start, next < 0 ? undefined : next);
    };

    it("Add halt point on every stop still to come, and Pause on the current one", () => {
      const html = render({ diagram: flightDiagram(flight({ cursor: 1 }), KINDS), ...handlers });
      expect(html).toContain('aria-label="Add halt point — “Merge”"');
      expect(html).toContain('aria-label="Add halt point — “Wait for me: Approve the merge”"');
      const cur = boxOf(html, "stop:s2");
      expect(cur).toContain('role="toolbar" aria-label="Pause or carry on at “Push”"');
      expect(cur).toMatch(/aria-label="Pause — “Push”" title="Pause the route where it is[^"]*">⏸</);
      expect(cur).toContain('aria-label="Add halt point — “Push”"');
      // The current stop's own toolbar never edits it — it can only come off.
      expect(cur).not.toContain("Edit “Push”");
      expect(cur).toMatch(/aria-label="Remove “Push” — the route pauses here" title="Remove this stop — the route pauses right before the stop that followed it">🗑</);
      // A stop that has flown: 🗑 alone.
      const flown = boxOf(html, "stop:s1");
      expect(flown).toContain('aria-label="Remove “Work the ticket” from the route"');
      expect(flown).not.toContain("Add halt point");
      expect(flown).not.toContain("Edit “");
    });

    it("without onToggleHalt: no Add halt point; without onRemove either, the current stop wears no toolbar (no headroom asked)", () => {
      const { onToggleHalt: _omit, ...rest } = handlers;
      const html = render({ diagram: flightDiagram(flight({ cursor: 1 }), KINDS), ...rest });
      expect(html).not.toContain("Add halt point");
      // Remove alone.
      expect(boxOf(html, "stop:s2")).toContain("Remove “Push” — the route pauses here");
      expect(boxOf(html, "stop:s2")).not.toContain("Pause");
      const bare = render({ diagram: flightDiagram(flight({ cursor: 1 }), KINDS), onSelect: () => {} });
      expect(boxOf(bare, "stop:s2")).not.toContain("fl-tools");
      // Its right-click menu still has Pause.
      expect(menuItemsFor(flightDiagram(flight({ cursor: 1 }), KINDS).nodes[1], { can: {}, live: true, here: true }).map((i) => i.label)).toContain("Pause");
    });

    it("a halt point wears the badge, its ⏸ pressed: Remove halt point", () => {
      const steps = STEPS.map((st) => (st.id === "s4" ? halting(st) : st));
      const html = render({ diagram: flightDiagram(flight({ cursor: 1, steps }), KINDS), ...handlers });
      const merge = boxOf(html, "stop:s4");
      expect(merge).toContain("fl-halt-point");
      expect(merge).toContain('aria-label="Merge, still to come, halt point"');
      expect(merge).toMatch(/<span class="fl-halt-mark" title="Halt point — [^"]*" aria-hidden="true">⏸<\/span>/);
      expect(merge).toMatch(/class="fl-icon-btn fl-tool fl-tool-halt fl-tool-on" aria-pressed="true" aria-label="Remove halt point — “Merge”"/);
      expect(boxOf(html, "stop:s3")).not.toContain("fl-halt-mark");
    });

    it("paused at a halt point: the paused box, the badge, and Carry on", () => {
      const steps = STEPS.map((st) => (st.id === "s2" ? halting(st) : st));
      const html = render({
        diagram: flightDiagram(flight({ state: "paused", reason: "halt point", cursor: 1, steps, step_states: { s1: { state: "done", since: 0, note: "", detail: {}, rounds: 0 } } }), KINDS),
        ...handlers,
      });
      const cur = boxOf(html, "stop:s2");
      expect(cur).toMatch(/class="fl-box fl-box-stop fl-st-paused[^"]*fl-halt-point/);
      expect(cur).toContain("fl-halt-mark");
      expect(cur).toMatch(/aria-label="Carry on — “Push”" title="Go on from this stop">▶</);
      expect(cur).toContain('aria-label="Remove halt point — “Push”"');
    });

    it("the editor's stops take Add halt point too (onToggleHalt)", () => {
      const html = render({ diagram: flywayDiagram(STEPS, KINDS), ...handlers });
      expect(html.match(/aria-label="Add halt point — /g)).toHaveLength(4);
      expect(html).not.toContain("Pause");
    });

    it("a run of flown stops folded into a pill: ✓ and its title, opened by a click, nothing to edit", () => {
      const d = flightDiagram(flight({ cursor: 1 }), KINDS);
      const flown = { ...d.nodes[0], id: "flown", kind: "flown", title: "1 flown", step: undefined, index: undefined } as unknown as DiagramNode;
      const html = render({ diagram: { ...d, nodes: [flown, ...d.nodes.slice(1)] }, ...handlers });
      const pill = boxOf(html, "flown");
      expect(pill).toContain('class="fl-box fl-box-flown"');
      expect(pill).toContain("✓");
      expect(pill).toContain('<span class="fl-box-title">1 flown</span>');
      expect(pill).not.toContain("fl-tools");
      expect(pill).not.toContain("fl-halt");
    });
  });
});
