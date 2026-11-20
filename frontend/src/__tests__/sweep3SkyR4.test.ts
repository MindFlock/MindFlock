/** UI sweep round 3, package R4 — the Sky's stops and saved routes:
 *  #16 removals ask where they can't be undone or the route is at them (■ on a
 *      stops-only route; the stop dialog's Remove / 🗑; a finished route's stop);
 *  #17 the default-stops editor's Discard and a built-in's Reset to default
 *      have an Undo;
 *  #18 Esc in the Sky's New session card keeps a typed prompt for next time;
 *  #24 the ContextMenu key / Shift+F10 opens a focused tile's menu;
 *  #25 "Edit its stops" in the catalog card's ⋯ too;
 *  #26 one save, one name ("Save as a route…") and one toast (`savedWords`);
 *  #27 ▶ on a stops-only route says "Starting…" until the server answers. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";
import type { DiagramNode } from "../lib/flightDiagram";
import type { FlightPlan, FlightStep } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/flightsApi", async (orig) => ({
  ...(await orig<typeof import("../lib/flightsApi")>()),
  resetPlan: vi.fn(),
  savePlan: vi.fn(),
}));

const API = await import("../lib/flightsApi");
const { toast } = await import("../lib/toast");
const { resetSavedRoute } = await import("../components/flights/StopCatalog");
const { StopDialog } = await import("../components/flights/StopDialog");
const { removeAskWords } = await import("../components/flights/useSessionLine");
const { isMenuKey } = await import("../components/flights/SkyChain");
const { newBlockDraft, keepNewBlockDraft } = await import("../components/flights/NewSessionBlock");
const { cardMenu } = await import("../lib/stopCatalog");
const { savedWords } = await import("../lib/groundedLines");

const step = (id: string, kind: string, title: string): FlightStep => ({ id, kind, title, config: { a: 1 }, on_fail: "halt" });

describe("#16 removals that ask", () => {
  it("■ on a stops-only route asks “Stop this route?” (the cancel question it already drew)", () => {
    const g = code("components/flights/GroundedLine.tsx");
    expect(g).toContain("onStop: cancel.ask,");
    expect(g).toContain("{cancel.node}");
    expect(g).not.toContain('controlFlight(flight.id, "cancel")');
  });

  it("the words: the stop the route is at, and a finished route's stop (it clears the whole route)", () => {
    expect(removeAskWords("remove", "Commit")).toEqual({
      title: "Remove “Commit”?",
      body: "The route is at it right now: it pauses before what follows. Undo puts it back.",
      confirm: "Remove",
    });
    expect(removeAskWords("clear", "Commit")).toEqual({ title: "Clear the finished route?", body: "Its record goes.", confirm: "Clear" });
  });

  it("the dialog's Remove, a group's Remove and a sub-step's 🗑 go through requestRemove, asked inside the dialog", () => {
    const s = code("components/flights/useSessionLine.tsx");
    expect(s).toContain("onRemoveSub={flight ? (s) => requestRemove(s, { inDialog: true }) : undefined}");
    expect(s).toContain("onRemove={flight || landedId ? () => requestRemove(open, { inDialog: true, then: () => setDialog(null) }) : undefined}");
    expect(s).toContain("(flight || landedId) && open.step ? () => requestRemove(open, { inDialog: true, then: close }) : undefined");
    expect(s.match(/confirm=\{dialogAsk\}/g)?.length).toBe(2);
    // A finished route's stop always asks where the question can show; the
    // stop the route is at asks as the tile does (removalNeedsAsk).
    expect(s).toContain('const kind = !flight && landedId ? "clear" : asks && removalNeedsAsk({ kind: "stops", indexes: nodeIndexes(n), flight }) ? "remove" : "";');
    // Nothing removes from the dialog without that check.
    expect(s).not.toContain("act(() => removeNode(s))");
    expect(s).not.toContain("if (await clearLanded()) setDialog(null);");
  });

  it("the dialog draws the question over its actions (a stop's and a group's)", () => {
    const ask = createElement("p", { className: "the-ask" }, "Asked");
    const stop: DiagramNode = { id: "stop:s1", kind: "stop", row: "main", title: "Commit", sub: "", state: "done", icon: "ship", index: 0, editable: false, step: step("s1", "ship", "Commit") } as DiagramNode;
    const one = renderToStaticMarkup(createElement(StopDialog, { portal: false, node: stop, onClose() {}, onRemove() {}, confirm: ask } as never));
    expect(one).toContain('<p class="the-ask">Asked</p>');
    expect(one.indexOf("the-ask")).toBeLessThan(one.indexOf("stop-dialog-actions"));
    const group: DiagramNode = {
      id: "group:g1",
      kind: "group",
      row: "main",
      title: "Ship it",
      sub: "",
      state: "pending",
      icon: "group",
      index: 0,
      editable: true,
      group: { id: "g1", name: "Ship it", plan_id: "", steps: [stop.step!], nodes: [stop], done: 0, total: 1, current: null },
    } as unknown as DiagramNode;
    const two = renderToStaticMarkup(createElement(StopDialog, { portal: false, node: group, onClose() {}, onRemove() {}, confirm: ask } as never));
    expect(two.indexOf("the-ask")).toBeGreaterThan(0);
    expect(two.indexOf("the-ask")).toBeLessThan(two.indexOf("stop-dialog-actions"));
    // No question: nothing extra.
    expect(renderToStaticMarkup(createElement(StopDialog, { portal: false, node: stop, onClose() {}, onRemove() {} } as never))).not.toContain("the-ask");
  });
});

describe("#17 resets with an Undo", () => {
  it("the default-stops editor's Discard (×, ⋯, Delete) is the plain Remove: Undo makes the editor again", () => {
    const g = code("components/flights/GroundedLine.tsx");
    expect(g).not.toContain("const discard =");
    expect(g).toContain("...(edits_default ? { edits_default } : {})");
    // The host's Remove (SkyView removeGrounded) already brings an editor back an editor.
    expect(code("components/flights/SkyView.tsx")).toContain("const g = await reground(steps, name, was, ed);");
  });

  beforeEach(() => {
    vi.mocked(toast).mockClear();
    vi.mocked(API.savePlan).mockReset();
    vi.mocked(API.resetPlan).mockReset();
  });

  it("Reset to default keeps the route as it was, and the toast's Undo saves it back", async () => {
    const plan = { id: "builtin:ticket", name: "Ticket to merge", builtin: true, steps: [step("s1", "agent", "Fix it")], updated: 5 } as unknown as FlightPlan;
    vi.mocked(API.resetPlan).mockResolvedValue({ ...plan, steps: [] });
    vi.mocked(API.savePlan).mockResolvedValue(plan);
    await resetSavedRoute(plan);
    expect(API.resetPlan).toHaveBeenCalledWith("builtin:ticket");
    const [msg, opts] = vi.mocked(toast).mock.calls[0];
    expect(msg).toBe("“Ticket to merge” is back to its default");
    expect(opts?.action?.label).toBe("Undo");
    opts!.action!.onClick();
    expect(API.savePlan).toHaveBeenCalledWith(plan);
    // A copy: a later change to the plan object doesn't reach the Undo.
    expect(vi.mocked(API.savePlan).mock.calls[0][0]).not.toBe(plan);
  });

  it("a refused reset says so, with no Undo", async () => {
    vi.mocked(API.resetPlan).mockRejectedValue(new Error("nope"));
    await resetSavedRoute({ id: "builtin:x", name: "X", builtin: true, steps: [] } as unknown as FlightPlan);
    const [msg, opts] = vi.mocked(toast).mock.calls[0];
    expect(msg).toBe("Reset failed: nope");
    expect(opts?.action).toBeUndefined();
  });

  it("the catalog's ⋯ and the shelf's menu both reset through it", () => {
    expect(code("components/flights/StopCatalog.tsx")).toContain("const reset = (plan: FlightPlan) => resetSavedRoute(plan);");
    expect(code("components/flights/StopShelf.tsx")).toContain('else if (key === "reset") await resetSavedRoute(plan);');
  });
});

describe("#18 Esc in the Sky's New session card keeps the prompt", () => {
  it("a typed prompt is kept; a blank one is no draft", () => {
    keepNewBlockDraft("fix the login bug");
    expect(newBlockDraft()).toBe("fix the login bug");
    keepNewBlockDraft("   ");
    expect(newBlockDraft()).toBe("");
  });

  it("the card opens with it, mirrors every change, and drops it once a session took it", () => {
    const b = code("components/flights/NewSessionBlock.tsx");
    expect(b).toContain("const [prompt, setPrompt] = useState(newBlockDraft);");
    expect(b).toContain("useEffect(() => keepNewBlockDraft(prompt), [prompt]);");
    const at = b.indexOf("title = await postInstance(body, device);");
    expect(b.slice(at, at + 200)).toContain('keepNewBlockDraft("");');
  });
});

describe("#24 the keyboard opens a tile's menu", () => {
  it("ContextMenu or Shift+F10 (no other modifier)", () => {
    expect(isMenuKey({ key: "ContextMenu", shiftKey: false })).toBe(true);
    expect(isMenuKey({ key: "F10", shiftKey: true })).toBe(true);
    expect(isMenuKey({ key: "F10", shiftKey: false })).toBe(false);
    expect(isMenuKey({ key: "F10", shiftKey: true, ctrlKey: true })).toBe(false);
    expect(isMenuKey({ key: "Enter", shiftKey: true })).toBe(false);
  });

  it("a focused tile opens the same menu, at the tile", () => {
    const c = code("components/flights/SkyChain.tsx");
    expect(c).toContain("} else if (isMenuKey(e) && p.onMenu) {");
    expect(c).toContain("p.onMenu(n.node, { clientX: r.left, clientY: r.bottom });");
  });
});

describe("#25 Edit its stops in the catalog's ⋯", () => {
  const plan = { id: "p_1", builtin: false };
  it("offered when the host can edit, right after Rename", () => {
    expect(cardMenu(plan, false, { edit: true }).map((i) => i.label)).toEqual(["Rename", "Edit its stops", "What starts it…", "☆ On the shelf", "Delete"]);
    expect(cardMenu(plan, false).map((i) => i.key)).not.toContain("edit");
  });

  it("the catalog hands it to the host; the shelf's catalog closes for the copy on the Sky", () => {
    const cat = code("components/flights/StopCatalog.tsx");
    expect(cat).toContain("cardMenu(plan, shelf ? shelf(c.drop) : false, { edit: !!onEditPlan })");
    expect(cat).toContain('else if (it.key === "edit") onEditPlan?.(plan);');
    const shelf = code("components/flights/StopShelf.tsx");
    expect(shelf).toContain("setCatalog(false);\n                  onEditPlan(plan);");
  });
});

describe("#26 one save, one name, one toast", () => {
  it("the selection bar and the group dialog say Save as a route", () => {
    expect(code("components/flights/SkySelection.tsx")).toContain("Save as a route…");
    expect(code("components/flights/StopDialog.tsx")).toContain("Save as a route\n");
    for (const f of ["components/flights/SkySelection.tsx", "components/flights/StopDialog.tsx", "components/flights/useSessionLine.tsx"]) expect(code(f), f).not.toContain("Save to the shelf");
  });

  it("every save toasts savedWords", () => {
    expect(code("components/flights/SkySelection.tsx")).toContain("toast(savedWords(saved.name, false));");
    expect(code("components/flights/useSessionLine.tsx")).toContain("toast(savedWords(saved?.name || name, !!plan.id));");
    for (const f of ["components/flights/SkySelection.tsx", "components/flights/useSessionLine.tsx"]) expect(code(f), f).not.toContain("” on the shelf\")");
    expect(savedWords("Ship it", false)).toBe("Saved “Ship it” — routes already running keep their own copy");
  });
});

describe("#27 ▶ says Starting… while it starts", () => {
  it("a stops-only route's ▶ waits, disabled, titled Starting…, till attachGrounded settles", () => {
    const g = code("components/flights/GroundedLine.tsx");
    expect(g).toContain('label: running ? "Stop this run" : starting ? "Starting…" : "Run these stops (⋯ Run on a session… to pick where)",');
    expect(g).toContain(".finally(() => setStarting(false));");
    const c = code("components/flights/SkyChain.tsx");
    expect(c).toContain("disabled={!r.running && !!r.starting}");
    expect(c).toContain("aria-busy={r.starting || undefined}");
  });

  it("the lane's Run says Starting… too, as the drawer's does", () => {
    expect(code("components/flights/IntakeLane.tsx")).toContain('{busy ? "Starting…" : "Run"}');
  });
});
