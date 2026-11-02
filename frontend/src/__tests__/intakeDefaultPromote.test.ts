/** A source's default stops, promoted from one ticket's line in the Sky.
 * Our built-in stops are every intake source's default; editing ONE ticket's
 * line never changes it — only the line's ⋯ "Use these stops for every
 * <ticket|PR|issue> from <source>" does. The pure words (per kind, the
 * source's name without the lane's kind prefix), when the item shows, what
 * pressing it sends (`from_flight`) and what its Undo sends back
 * (`previous_plan_id`), and how the line's ⋯ wires it. */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { code, css } from "./sourceText";
import type { IntakeDefault } from "../lib/flights";

const setIntakeDefault = vi.fn();
const intakeSourceOfFlight = vi.fn();
vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/flightsApi", async (orig) => ({
  ...(await orig<typeof import("../lib/flightsApi")>()),
  setIntakeDefault: (...a: unknown[]) => setIntakeDefault(...a),
  intakeSourceOfFlight: (...a: unknown[]) => intakeSourceOfFlight(...a),
}));

const L = await import("../components/flights/useSessionLine");
const { toast } = await import("../lib/toast");
const U = await import("../lib/skyUndo");

const src = (kind: IntakeDefault["kind"], label: string, target = "t1"): Pick<IntakeDefault, "kind" | "label" | "target"> => ({ kind, label, target });

describe("when the item shows", () => {
  it("only on a flight Intake started", () => {
    expect(L.isIntakeSubject("ticket")).toBe(true);
    expect(L.isIntakeSubject("pr")).toBe(true);
    expect(L.isIntakeSubject("issue")).toBe(true);
    expect(L.isIntakeSubject("manual")).toBe(false);
    expect(L.isIntakeSubject("schedule")).toBe(false);
    expect(L.isIntakeSubject(undefined)).toBe(false);
  });
});

describe("the words", () => {
  it("per kind: one item, many items", () => {
    expect(L.intakeWords("tickets")).toEqual({ one: "ticket", many: "tickets" });
    expect(L.intakeWords("prs")).toEqual({ one: "PR", many: "PRs" });
    expect(L.intakeWords("issues")).toEqual({ one: "issue", many: "issues" });
  });

  it("the source's name drops the lane's kind prefix, else stays", () => {
    expect(L.intakeSourceName(src("tickets", "Tickets · Shortcut"))).toBe("Shortcut");
    expect(L.intakeSourceName(src("prs", "PRs · o/r"))).toBe("o/r");
    expect(L.intakeSourceName(src("issues", "Issues · acme/web"))).toBe("acme/web");
    expect(L.intakeSourceName(src("tickets", "Linear"))).toBe("Linear");
    expect(L.intakeSourceName(src("tickets", "", "jira-1"))).toBe("jira-1");
  });

  it("label, tooltip and toast", () => {
    const w = L.promoteWords(src("tickets", "Tickets · Shortcut"));
    expect(w.label).toBe("Use these stops for every ticket from Shortcut");
    expect(w.done).toBe("New tickets from Shortcut fly these stops");
    // It changes what NEW items fly; this one keeps running as it is.
    expect(w.title).toContain("NEW tickets from Shortcut");
    expect(w.title).toContain("This ticket keeps running as it is");
    const p = L.promoteWords(src("prs", "PRs · o/r"));
    expect(p.label).toBe("Use these stops for every PR from o/r");
    expect(p.done).toBe("New PRs from o/r fly these stops");
    expect(L.promoteWords(src("issues", "Issues · a/b")).label).toBe("Use these stops for every issue from a/b");
  });
});

describe("pressing it", () => {
  beforeEach(() => {
    setIntakeDefault.mockReset();
    vi.mocked(toast).mockReset();
    U.clearUndo();
  });

  it("sends from_flight, toasts with Undo, and the Undo puts the previous stops back", async () => {
    setIntakeDefault.mockResolvedValueOnce({ kind: "tickets", target: "sc", label: "Tickets · Shortcut", plan_id: "p-new", name: "x", own: true, steps: [], previous_plan_id: "p-old" });
    expect(await L.promoteStops("f1")).toBe(true);
    expect(setIntakeDefault).toHaveBeenCalledWith({ from_flight: "f1" });
    const [msg, opts] = vi.mocked(toast).mock.calls[0];
    expect(msg).toBe("New tickets from Shortcut fly these stops");
    expect(opts?.action?.label).toBe("Undo");
    // The same entry Ctrl+Z pops.
    expect(U.undoCount()).toBe(1);
    setIntakeDefault.mockResolvedValueOnce(null);
    opts!.action!.onClick();
    expect(setIntakeDefault).toHaveBeenLastCalledWith({ kind: "tickets", target: "sc", plan_id: "p-old" });
    expect(U.undoCount()).toBe(0);
  });

  it("back to our built-in when the source had none of its own", async () => {
    setIntakeDefault.mockResolvedValueOnce({ kind: "prs", target: "o/r", label: "PRs · o/r", plan_id: "p", name: "x", own: true, steps: [], previous_plan_id: "" });
    await L.promoteStops("f2");
    setIntakeDefault.mockResolvedValueOnce(null);
    await U.popUndo()!.undo();
    expect(setIntakeDefault).toHaveBeenLastCalledWith({ kind: "prs", target: "o/r", plan_id: "" });
  });

  it("a refusal: no toast, nothing to undo", async () => {
    setIntakeDefault.mockResolvedValueOnce(null);
    expect(await L.promoteStops("f3")).toBe(false);
    expect(toast).not.toHaveBeenCalled();
    expect(U.undoCount()).toBe(0);
  });
});

describe("wiring", () => {
  const hook = code("components/flights/useSessionLine.tsx");
  const line = code("components/flights/SkyLine.tsx");

  it("fetches the source once per flight id, only for an intake flight", () => {
    expect(hook).toContain('queryKey: ["intake-source-of-flight", id]');
    expect(hook).toContain("queryFn: () => intakeSourceOfFlight(id)");
    expect(hook).toContain("enabled: intake");
    expect(hook).toContain("staleTime: Infinity");
  });

  it("the line's ⋯ carries the item beside its other flight items", () => {
    expect(line).toContain("const promote = usePromoteStops(line.flight)");
    expect(line).toMatch(/\.\.\.\(promote \? \[\{ key: "promote", label: promote\.label, title: promote\.title, className: "skl-mi-promote", onSelect: \(\) => void promote\.run\(\) \}\] : \[\]\)/);
    const at = (k: string) => line.indexOf('key: "' + k + '"');
    expect(at("flyway")).toBeGreaterThan(0);
    expect(at("promote")).toBeGreaterThan(at("flyway"));
    expect(at("close")).toBeGreaterThan(at("promote"));
  });

  it("a long source name wraps instead of widening the menu", () => {
    const rule = css("components/flights/SkyLine.css");
    expect(rule).toMatch(/\.vf-menu-item\.skl-mi-promote\s*\{[^}]*white-space:\s*normal/);
  });
});
