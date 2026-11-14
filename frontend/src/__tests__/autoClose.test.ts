/** Finished sub-sessions close themselves (lib/autoClose, backend
 * core/auto_close.py): the "Closed N finished workers of <parent>" toast and
 * its Undo (reopen + keep open), the ⋯ menu's Keep open switch, and the
 * orphan line ("parent closed — review or close"). */

/// <reference types="vite/client" />
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Caps, Instance } from "../api/types";
import { code } from "./sourceText";

const toastMock = vi.fn();
const apiMock = vi.fn();
const instApiMock = vi.fn();
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toastMock(...a) }));
vi.mock("../lib/errorPop", () => ({ errorPop: vi.fn(), confirmPop: vi.fn() }));
vi.mock("../lib/terminals", () => ({ focusTerm: vi.fn(), releaseTerms: vi.fn() }));
vi.mock("../api/client", async (orig) => ({
  ...((await orig()) as object),
  api: (...a: unknown[]) => apiMock(...a),
  instApi: (...a: unknown[]) => instApiMock(...a),
}));

const ac = await import("../lib/autoClose");
const { actionCtx } = await import("../lib/sessionActionTable");
const { sessionMenu } = await import("../lib/sessionMenu");

const CAPS: Caps = { git: true, tailscale: true, ticketing: true, github: true };
const row = (o: Partial<Instance> = {}) =>
  ({ title: "w1", status: "ready", branch: "me/w1", path: "/w/repo/w1", git_repo: true, ...o }) as Instance;
const sessionItems = (o: Partial<Instance> = {}) => {
  const inst = row(o);
  const m = sessionMenu(inst, actionCtx(inst.title, { inst, caps: CAPS }));
  return m.groups.find((g) => g.id === "session")?.items || [];
};
const WORKER = { keep_open: false, orphan_of: "", closes_at: null, why: "" };

beforeEach(() => {
  toastMock.mockReset();
  apiMock.mockReset();
  // Anything else the menu asks (the IDE probe) never answers.
  apiMock.mockImplementation(() => new Promise(() => {}));
  instApiMock.mockReset();
});
afterEach(() => vi.clearAllMocks());

describe("the auto-close toast", () => {
  const data = {
    parent: "lead",
    count: 2,
    titles: ["w1", "w2"],
    entries: [
      { title: "w1", id: "w1-1" },
      { title: "w2", id: "w2-1" },
    ],
  };

  it("names how many finished workers closed, and whose", () => {
    expect(ac.autoClosedToastText(data, (t) => t)).toBe("Closed 2 finished workers of “lead”");
    expect(ac.autoClosedToastText({ parent: "lead", titles: ["w1"] }, (t) => t)).toBe(
      "Closed finished worker “w1” of “lead”"
    );
    expect(ac.autoClosedToastText({ parent: "", titles: ["a", "b"] }, (t) => t)).toBe("Closed 2 finished workers");
  });

  it("carries an Undo that reopens exactly those entries and keeps them open", async () => {
    apiMock.mockResolvedValue({ reopened: ["w1", "w2"], failed: {} });
    ac.autoClosedToast(data);
    expect(toastMock).toHaveBeenCalledTimes(1);
    const [text, opts] = toastMock.mock.calls[0] as [string, { action?: { label: string; onClick(): void } }];
    expect(text).toContain("Closed 2 finished workers");
    expect(opts.action?.label).toBe("Undo");
    opts.action!.onClick();
    await vi.waitFor(() => expect(apiMock).toHaveBeenCalled());
    expect(apiMock).toHaveBeenCalledWith("/api/auto-close/undo", { method: "POST", json: { ids: ["w1-1", "w2-1"] } });
    await vi.waitFor(() => expect(toastMock).toHaveBeenCalledTimes(2));
    expect(toastMock.mock.calls[1][0]).toContain("kept open");
  });

  it("offers no Undo when nothing came back to reopen", () => {
    ac.autoClosedToast({ parent: "lead", titles: ["w1"] });
    const opts = toastMock.mock.calls[0][1] as { action?: unknown };
    expect(opts.action).toBeUndefined();
  });

  it("is shown by EventToasts for the server's event, never for a replay", () => {
    const src = code("components/EventToasts.tsx");
    expect(ac.AUTO_CLOSED_EVENT).toBe("system.subsessions_closed");
    expect(src).toContain("ev.subscribe(AUTO_CLOSED_EVENT");
    const at = src.indexOf("ev.subscribe(AUTO_CLOSED_EVENT");
    expect(src.slice(at, at + 200)).toContain("if (isReplay(env)) return;");
  });
});

describe("Keep open in the ⋯ menu", () => {
  it("is offered only on a worker the sweep may close", () => {
    expect(sessionItems().map((i) => i.key)).not.toContain("keep-open");
    expect(sessionItems({ auto_close: WORKER }).map((i) => i.key)).toContain("keep-open");
    // Another device's row is changed where it lives.
    expect(sessionItems({ auto_close: WORKER, device: "mac" } as Partial<Instance>).map((i) => i.key)).not.toContain(
      "keep-open"
    );
  });

  it("turns Keep open on, and shows ✓ once it is on (clicking again turns it off)", async () => {
    instApiMock.mockResolvedValue({ title: "w1", keep_open: true });
    const item = sessionItems({ auto_close: WORKER }).find((i) => i.key === "keep-open")!;
    expect(item.label).toBe("Keep open");
    await item.run();
    expect(instApiMock).toHaveBeenCalledWith("w1", "/keep-open", { method: "POST", json: { keep: true } });

    const on = sessionItems({ auto_close: { ...WORKER, keep_open: true } }).find((i) => i.key === "keep-open")!;
    expect(on.label).toBe("✓ Keep open");
    expect(on.checked).toBe(true);
    await on.run();
    expect(instApiMock).toHaveBeenLastCalledWith("w1", "/keep-open", { method: "POST", json: { keep: false } });
  });
});

describe("the orphan line", () => {
  it("says the parent closed, only while the server flags it", () => {
    expect(ac.orphanLine(row({ auto_close: WORKER }))).toBeNull();
    const line = ac.orphanLine(row({ auto_close: { ...WORKER, orphan_of: "lead" } }));
    expect(line?.text).toBe("parent closed — review or close");
    expect(line?.title).toContain("lead");
    expect(code("components/sidebar/SidebarRow.tsx")).toContain("orphanLine(inst)");
  });
});

describe("the setting", () => {
  it("is a switch under Agent orchestration → Workers, with the grace minutes", () => {
    const src = code("components/settings/screens/AgentOrchestration.tsx");
    expect(src).toContain('data-field="auto_close_children"');
    expect(src).toContain('field="auto_close_grace_minutes"');
    expect(src).toContain("<AutoCloseRows />");
  });
});
