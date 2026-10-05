/** Regressions from the MCP UX v1 review (2026-10-05): every paste re-checked
 * on the server, one paste in flight per session, remote rows get no
 * playbooks, one "children of" rule for every surface, and a "wrap up" chip
 * that respects the fork button's block reason. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Caps, Instance } from "../api/types";

const api = vi.fn();
const toast = vi.fn();
vi.mock("../api/client", async (orig) => ({
  ...((await orig()) as object),
  api: (...a: unknown[]) => api(...a),
}));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
vi.mock("../lib/sessionActions", () => ({ selectSession: () => {}, instances: () => [] }));
vi.mock("../lib/terminals", () => ({ focusTerm: () => {} }));

const { liveChildren, mcpCapable, pastePlaybook, RESTART_REASON } = await import("../lib/playbooks");
const { childrenByParent, childrenOf, parentChip } = await import("../lib/agentMessages");
const { familyOf } = await import("../lib/thread");

beforeEach(() => {
  api.mockReset();
  toast.mockReset();
});

describe("pastePlaybook", () => {
  it("types with dialog_safe, so the server refuses to type into a dialog", async () => {
    api.mockResolvedValueOnce({ text: "Wrap up worker api-billing-2" }).mockResolvedValueOnce({});
    expect(await pastePlaybook("api", { id: "wrapup", label: "Wrap up workers" })).toBe(true);
    expect(api.mock.calls[1]).toEqual([
      "/api/instances/api/send",
      { json: { text: "Wrap up worker api-billing-2", submit: false, dialog_safe: true } },
    ]);
  });

  it("a second paste while one is in flight types nothing (any surface)", async () => {
    let done!: (v: unknown) => void;
    api.mockReturnValueOnce(new Promise((r) => (done = r))).mockResolvedValueOnce({});
    const first = pastePlaybook("api", { id: "workers", label: "Check on workers" });
    expect(await pastePlaybook("api", { id: "workers", label: "Check on workers" })).toBe(false);
    done({ text: "Check on your MindFlock workers" });
    expect(await first).toBe(true);
    expect(api).toHaveBeenCalledTimes(2); // one render, one send
  });

  it("says the server's reason when the render refuses (409)", async () => {
    api.mockRejectedValueOnce(new Error("Answer its prompt first — pasting now would answer the dialog"));
    expect(await pastePlaybook("api", { id: "wrapup", label: "Wrap up workers" })).toBe(false);
    expect(String(toast.mock.calls[0][0])).toContain("Answer its prompt first");
  });
});

describe("another device's sessions get no playbooks", () => {
  const caps = { agent_mcp: { enabled: true, providers: ["claude"] } } as Partial<Caps>;
  it("mcpCapable is false for a remote row (/api/playbooks is not forwarded)", () => {
    expect(mcpCapable(caps, { title: "api", provider: "claude" } as Instance)).toBe(true);
    expect(mcpCapable(caps, { title: "dev::api", provider: "claude" } as Instance)).toBe(false);
    expect(mcpCapable(caps, { title: "api", provider: "claude", device: "dev" } as Instance)).toBe(false);
  });
});

describe("one children-of rule for the menu, the palette, the roll-up and the composer", () => {
  const rows = [
    { title: "api" },
    { title: "w1", parent: "api" },
    { title: "w2", parent: "api", pending: true },
    { title: "dev::w3", parent: "api", device: "dev" },
    { title: "x", parent: "x" },
  ] as Instance[];

  it("every helper agrees: local, not pending, parent is it", () => {
    const want = ["w1"];
    expect(childrenOf("api", rows).map((r) => r.title)).toEqual(want);
    expect(liveChildren("api", rows).map((r) => r.title)).toEqual(want);
    expect((childrenByParent(rows).get("api") ?? []).map((r) => r.title)).toEqual(want);
    expect(familyOf("api", rows).children.map((r) => r.title)).toEqual(want);
    expect(childrenOf("x", rows)).toEqual([]);
  });
});

describe("the wrap up chip respects the fork button's block reason", () => {
  const done = { title: "w1", activity: "idle", last_report: { id: "m", status: "done", summary: "", ts: 1 } };
  it("is a label with the reason, never a one-click paste, when the parent can't take one", () => {
    const c = parentChip({ title: "api", activity: "idle", status: "running" }, [done], (t) => t, undefined, RESTART_REASON)!;
    expect(c.kind).toBe("blocked");
    expect(c.title).toContain(RESTART_REASON);
    const ok = parentChip({ title: "api", activity: "idle", status: "running" }, [done], (t) => t)!;
    expect(ok.kind).toBe("wrap");
  });
});
