/** The session ⋯ menu's Session group: Rename…, then Move to a folder…
 * (docs/flights.md "Moving a session") — this machine's own rows only, and
 * greyed with "moving…" while a move is under way. */

/// <reference types="vite/client" />
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Caps, Instance } from "../api/types";

vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/errorPop", () => ({ errorPop: vi.fn(), confirmPop: vi.fn() }));
vi.mock("../lib/terminals", () => ({ focusTerm: vi.fn(), releaseTerms: vi.fn() }));
vi.mock("../api/client", async (orig) => ({
  ...((await orig()) as object),
  api: () => new Promise(() => {}),
}));

const { actionCtx } = await import("../lib/sessionActionTable");
const { sessionMenu } = await import("../lib/sessionMenu");
const { useUi } = await import("../state/store");

const CAPS: Caps = { git: true, tailscale: true, ticketing: true, github: true };
const row = (o: Partial<Instance> = {}) =>
  ({ title: "s", status: "ready", branch: "me/s", path: "/w/repo/s", git_repo: true, ...o }) as Instance;
const sessionGroup = (o: Partial<Instance> = {}) => {
  const inst = row(o);
  const m = sessionMenu(inst, actionCtx(inst.title, { inst, caps: CAPS }));
  return m.groups.find((g) => g.id === "session")?.items || [];
};
const allKeys = (o: Partial<Instance> = {}) => {
  const inst = row(o);
  const m = sessionMenu(inst, actionCtx(inst.title, { inst, caps: CAPS }));
  return [...m.groups.flatMap((g) => g.items), ...m.more].map((i) => i.key);
};

afterEach(() => useUi.getState().closeDialog());

describe("Move to a folder…", () => {
  it("sits right after Rename… in the Session group", () => {
    const items = sessionGroup();
    const keys = items.map((i) => i.key);
    expect(keys.indexOf("move")).toBe(keys.indexOf("rename") + 1);
    const move = items.find((i) => i.key === "move")!;
    expect(move.label).toBe("Move to a folder…");
    expect(move.disabled).toBeFalsy();
    expect(move.title).toBe("Pauses the agent, moves the session's folder somewhere new, and resumes it there");
  });

  it("opens the move dialog on the session", () => {
    sessionGroup().find((i) => i.key === "move")!.run();
    expect(useUi.getState().openDialog).toBe("move");
    expect(useUi.getState().dialogTarget).toBe("s");
  });

  it("is greyed, saying moving…, while a move is under way", () => {
    const move = sessionGroup({ moving: true } as Partial<Instance>).find((i) => i.key === "move")!;
    expect(move.disabled).toBe(true);
    expect(move.note).toBe("moving…");
  });

  it("is left out for another device's row, a peer's shared folder, a session still starting and a gone folder", () => {
    expect(allKeys({ title: "mac::s", device: "mac" } as Partial<Instance>)).not.toContain("move");
    expect(allKeys({ peer_share: { link: "x" } } as unknown as Partial<Instance>)).not.toContain("move");
    expect(allKeys({ pending: true })).not.toContain("move");
    expect(allKeys({ workspace_missing: true } as Partial<Instance>)).not.toContain("move");
  });
});
