/** Sky and routes clarity, round 4 — one vocabulary and honest menus: the
 * stop the route is at offers Edit when it failed (its dialog saves, Retry
 * runs it) and no "can't change" note; Remove there says where the route
 * pauses; Skip / Done say the route goes on (not the bird); a halt point
 * reads "before <stop>" in the bell, the session's hint and the phone, as
 * on the Sky's bar; the zoom bar's buttons have names a screen reader can
 * say. The phone, the CLI and the MCP: tests/unit/test_flight_plain_errors.py. */

import { describe, expect, it } from "vitest";
import type { DiagramNode } from "../lib/flightDiagram";
import type { FlightStep } from "../lib/flights";
import { code } from "./sourceText";

const { menuItemsFor } = await import("../lib/stopEditing");
const { pausedAtLine } = await import("../lib/needsYou");

const push = { id: "s2", kind: "ship", title: "Push", config: { lane: "push" }, on_fail: "halt" } as FlightStep;
const node = (over: Partial<DiagramNode> = {}): DiagramNode =>
  ({ id: "stop:s2", kind: "stop", row: "main", title: "Push", sub: "", state: "current", icon: "ship", step: push, index: 1, editable: false, ...over }) as DiagramNode;
const ctx = { can: { edit: true, remove: true, halt: true }, live: true, here: true };
const items = (n: DiagramNode) => menuItemsFor(n, ctx);

describe("the stop the route is at: its menu tells the truth", () => {
  it("a failed stop: Edit (Save, then Retry runs it), no 'can't change' note", () => {
    const m = items(node({ state: "halted" }));
    expect(m.find((i) => i.key === "edit")).toMatchObject({ label: "Edit" });
    expect(m.some((i) => i.note)).toBe(false);
  });

  it("a running one stays read-only: Details and the note", () => {
    const m = items(node({ state: "current" }));
    expect(m.find((i) => i.key === "edit")!.label).toBe("Details");
    expect(m.at(-1)!.label).toBe("The route is here — it can't change now");
  });

  it("Remove says where the route pauses; Skip and Done say the route goes on", () => {
    expect(items(node({ state: "current" })).find((i) => i.key === "remove")!.label).toBe("Remove — pauses before the next stop");
    expect(items(node({ state: "halted" })).find((i) => i.key === "skip")!.title).toBe("Skip this stop — the route goes on without it");
    expect(items(node({ state: "perched", step: { ...push, kind: "you", title: "Approve" } as FlightStep })).find((i) => i.key === "done")!.title).toBe("Mark this stop done — the route goes on");
    expect(code("lib/stopEditing.ts")).not.toContain("the bird flies on");
  });
});

describe("a halt point reads 'before' everywhere", () => {
  it("the bell's row, the session's hint and the phone's mirror", () => {
    expect(pausedAtLine("Push")).toBe("Paused before Push — Carry on");
    expect(pausedAtLine("")).toBe("Paused at its halt point — Carry on");
    expect(code("lib/sessionState.ts")).toContain('reached its halt point before "');
    // The phone (static/mobile.js) mirrors these: tests/unit/test_flight_plain_errors.py.
  });
});

describe("names a screen reader can say", () => {
  it("the zoom bar's 1, Fit and 100%", () => {
    const v = code("components/flights/SkyView.tsx");
    expect(v).toContain('className="sky-zoom-btn sky-fit-one" aria-label="Fit the focused route"');
    expect(v).toContain('aria-label="Fit all routes"');
    expect(v).toContain('className="sky-zoom-btn sky-zoom-100" aria-label="Zoom to 100%"');
  });
});
