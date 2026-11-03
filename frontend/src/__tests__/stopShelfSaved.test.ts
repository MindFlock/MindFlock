/** The shelf's saved stops (StopShelf): the Multi-step region, after the
 * Blocks and before All blocks…, the saved stops (`useFlightPlans`, built-ins
 * first) — the name and a dot per step (a
 * perch's gold, "+N" past ten), dragged with FLYWAY_MIME = the id; the
 * right-click menu (Rename in the chip, Takes off…, Reset to default on a
 * built-in, Delete on yours, confirmed inline); the rename rules; and the
 * Takes off dialog (the trigger, schedule and session defaults, in a Modal).
 * Server-rendered with the kinds and plans seeded in a QueryClient. */

import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";
import type { FlightPlan, FlightStep, StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
// The Takes off dialog is portaled to <body>; a string render has no DOM, so
// the portal renders its children in place.
vi.mock("react-dom", async (orig) => ({ ...(await orig<typeof import("react-dom")>()), createPortal: (node: unknown) => node }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { FLYWAY_MIME } = await import("../lib/skyLayout");
const { StopShelf, savedMenuItems } = await import("../components/flights/StopShelf");
const { savedChainLabel, savedDragData } = await import("../lib/stopCatalog");
const { TakeoffDialog, triggerCron } = await import("../components/flights/TakeoffDialog");
const { NAME_MAX, checkRename, planDots, savedStopOrder } = await import("../lib/groundedLines");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const kind = (k: string, label: string, actor: "robot" | "you" = "robot"): StepKindDef => ({ kind: k, label, actor, summary: "", fields: [], default_title: label });
const KINDS = [kind("agent", "Agent"), kind("ship", "Ship"), kind("you", "You", "you")];
const step = (i: number, k = "ship"): FlightStep => ({ id: "s" + i, kind: k, title: "Step " + i, config: {}, on_fail: "halt" });
const plan = (id: string, name: string, steps: FlightStep[], builtin = false): FlightPlan => ({
  id,
  name,
  description: "",
  builtin,
  trigger: { kind: "manual", schedule: null, paused: false },
  session: { repo_path: "", program: "", model: "", in_place: false },
  steps,
  updated: 1,
});
const MINE = plan("p_mine", "Mine", [step(1, "agent"), step(2, "you"), step(3)]);
const TICKET = plan("builtin:ticket", "Ticket to merge", [step(1, "agent"), step(2), step(3), step(4), step(5), step(6), step(7), step(8), step(9), step(10), step(11), step(12, "you")], true);

/** `plans` null: the saved stops haven't loaded. */
function render(el: ReactElement, plans: FlightPlan[] | null = [MINE, TICKET]) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  c.setQueryData(["flight-kinds"], KINDS);
  if (plans) c.setQueryData(["flight-plans"], plans);
  c.setQueryData(["settings"], {});
  // The owner's shelf pick: these tests put the saved stops ON the shelf
  // (by default none is — they wait in the catalog).
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => (k === "mf_sky_shelf" ? JSON.stringify({ kinds: ["agent", "you", "ship", "verify", "wait"], plans: ["builtin:ticket", "builtin:pr", "p_mine"] }) : null),
    setItem: () => {},
  });
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}

describe("the saved stops on the shelf", () => {
  const html = render(createElement(StopShelf, {}));

  it("the Multi-step region: after the kinds and a divider, built-ins first, each draggable with its id, then All blocks…", () => {
    const ids = [...html.matchAll(/data-plan="([^"]+)"/g)].map((m) => m[1]);
    expect(ids).toEqual(["builtin:ticket", "p_mine"]);
    expect(html).toContain('<span class="sky-stops-divider" aria-hidden="true"></span><div class="sky-stops-region" data-region="multi" role="list" aria-label="Saved chains"><span class="sky-stops-label" aria-hidden="true">Saved chains</span>');
    expect(html.indexOf('data-region="multi"')).toBeGreaterThan(html.indexOf('data-kind="ship"'));
    expect(html.indexOf('data-plan="builtin:ticket"')).toBeGreaterThan(html.indexOf('data-region="multi"'));
    expect(html.indexOf("sky-catalog-chip")).toBeGreaterThan(html.indexOf('data-plan="p_mine"'));
    expect(html).toMatch(/<span class="sky-stop-chip sky-saved-chip builtin drag-only" data-plan="builtin:ticket" draggable="true"/);
    expect(html).toContain('<span class="sky-stop-name">Mine</span>');
    // what it is, in the house words: "Saved chain · N stops", then its stops
    expect(html).toMatch(/data-plan="p_mine" draggable="true" title="Saved chain · 3 stops: [^"\n]+ → [^"\n]+\n/);
    expect(savedChainLabel(1)).toBe("Saved chain · 1 stop");
  });

  it("a dot per step, a perch's gold, +N past ten", () => {
    const mine = html.slice(html.indexOf('data-plan="p_mine"'));
    expect(mine).toContain('<span class="sky-saved-dots" aria-hidden="true"><i class=""></i><i class="perch"></i><i class=""></i></span>');
    const ticket = html.slice(html.indexOf('data-plan="builtin:ticket"'), html.indexOf('data-plan="p_mine"'));
    expect(ticket.match(/<i /g)).toHaveLength(10);
    expect(ticket).toContain('<span class="sky-saved-more">+2</span>');
    expect(planDots(TICKET.steps, KINDS).dots.filter((d) => d.perch)).toHaveLength(0);
    expect(planDots(MINE.steps, KINDS)).toEqual({ dots: [{ perch: false }, { perch: true }, { perch: false }], more: 0 });
  });

  it("its drag carries FLYWAY_MIME = the id, the name as text (the MIME the canvas reads)", () => {
    expect(FLYWAY_MIME).toBe("application/x-mindflock-flyway");
    expect(savedDragData(MINE)).toEqual([
      ["application/x-mindflock-flyway", "p_mine"],
      ["text/plain", "Mine"],
    ]);
  });

  it("a click adds it to the focused chain when the host takes it", () => {
    const tap = render(createElement(StopShelf, { onTackPlan() {} }));
    expect(tap).toMatch(/<button type="button" class="sky-stop-chip sky-saved-chip" data-plan="p_mine"/);
    expect(tap).toContain('aria-label="Add “Mine” to the focused chain"');
  });

  it("round 13 — with onPlace a click puts it on the board instead", () => {
    const tap = render(createElement(StopShelf, { onPlace() {}, onTackPlan() {} }));
    expect(tap).toMatch(/<button type="button" class="sky-stop-chip sky-saved-chip" data-plan="p_mine"/);
    expect(tap).toContain('aria-label="Put “Mine” on the board"');
    expect(tap).toContain("Click to put it on the board, or drag it onto a chain or the sky — right-click for more");
    expect(tap).not.toContain("to the focused chain");
  });

  it("no saved stops (or not loaded yet): no Multi-step region, one divider before All blocks…", () => {
    for (const none of [[], null]) {
      const h = render(createElement(StopShelf, {}), none);
      expect(h).not.toContain("sky-saved-chip");
      expect(h).not.toContain('data-region="multi"');
      expect(h.match(/sky-stops-divider/g)).toHaveLength(1);
    }
  });
});

describe("a saved stop's menu", () => {
  it("Rename and What starts it… on every one; Reset on a built-in, Delete on yours", () => {
    expect(savedMenuItems(TICKET).map((i) => i.label)).toEqual(["Rename", "What starts it…", "Take off the shelf", "Reset to default"]);
    expect(savedMenuItems(MINE).map((i) => i.label)).toEqual(["Rename", "What starts it…", "Take off the shelf", "Delete"]);
    expect(savedMenuItems(MINE)[3].danger).toBe(true);
  });

  it("is right-click (or the context-menu key); acts through the plan routes; Delete confirms inline, never a native dialog", () => {
    const src = code("components/flights/StopShelf.tsx");
    expect(src).toContain("onContextMenu: menuAt,");
    expect(src).toContain('if (e.key !== "ContextMenu" && !(e.shiftKey && e.key === "F10")) return;');
    expect(src).toContain("await resetPlan(plan.id);");
    expect(src).toContain("await deletePlan(deleting.id);");
    expect(code("components/flights/StopCatalog.tsx")).toContain("await savePlan({ ...plan, name: c.name });");
    expect(src).toContain("<InlineConfirm");
    expect(src).toContain("<TakeoffDialog plan={takeoff}");
    expect(src).not.toMatch(/\bwindow\.(prompt|alert|confirm)\(|\bprompt\(/);
  });

  it("Rename is a field in the chip — the same field a catalog card uses (PlanNameField)", () => {
    expect(code("components/flights/StopShelf.tsx")).toContain("<PlanNameField plan={plan} plans={plans} onDone={onRenamed} />");
    const field = code("components/flights/StopCatalog.tsx");
    expect(field).toMatch(/className="sky-saved-rename fl-input"[\s\S]*?aria-label=\{"Rename “" \+ plan\.name \+ "”"\}/);
    expect(field).toMatch(/const c = checkRename\(name, plans, plan\.id\);/);
  });
});

describe("rename rules (checkRename)", () => {
  const plans = [MINE, TICKET];
  it("trimmed; empty, too long, or another's name refused; its own name fine", () => {
    expect(checkRename("  Ship it  ", plans, "p_mine")).toEqual({ ok: true, name: "Ship it" });
    expect(checkRename("   ", plans, "p_mine")).toEqual({ ok: false, error: "A saved chain needs a name" });
    expect(checkRename("x".repeat(NAME_MAX + 1), plans, "p_mine").ok).toBe(false);
    expect(checkRename("ticket TO merge", plans, "p_mine")).toEqual({ ok: false, error: "Another saved chain is already called “Ticket to merge”" });
    expect(checkRename("Mine", plans, "p_mine")).toEqual({ ok: true, name: "Mine" });
  });
  it("built-ins first, then yours", () => {
    expect(savedStopOrder([MINE, TICKET]).map((p) => p.id)).toEqual(["builtin:ticket", "p_mine"]);
  });
});

describe("Takes off… (TakeoffDialog)", () => {
  it("a Modal: its heading, the trigger chips, Save and Cancel", () => {
    const html = render(createElement(TakeoffDialog, { plan: MINE, onClose() {} }));
    expect(html).toContain('id="takeoff-dialog"');
    expect(html).toContain("<h2>When “Mine” starts</h2>");
    expect(html).toContain(">Starts<");
    expect(html).toContain('class="modal-actions"');
    expect(html).toContain(">Cancel</button>");
    expect(html).toContain(">Save</button>");
    // By hand: the repository and agent its sessions start in.
    expect(html).toContain("Repository");
  });

  it("a schedule shows its when; the cron the server previews", () => {
    const sched = { ...MINE, trigger: { kind: "schedule" as const, schedule: { at: "21:00", days: "weekdays" as const }, paused: false } };
    const html = render(createElement(TakeoffDialog, { plan: sched, onClose() {} }));
    expect(html).toContain("Weekdays");
    expect(html).toContain("Paused");
    expect(triggerCron(sched)).toBe("0 21 * * 1-5");
    expect(triggerCron(MINE)).toBe("");
  });

  it("saves with savePlan; a refusal stays in the dialog", () => {
    const src = code("components/flights/TakeoffDialog.tsx");
    expect(src).toContain("const saved = await savePlan(draft);");
    expect(src).toContain("setError(errMsg(err));");
    expect(src).toContain('<Modal id="takeoff-dialog" portal onClose={onClose}>');
  });
});
