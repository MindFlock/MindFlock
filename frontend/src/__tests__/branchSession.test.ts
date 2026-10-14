/** Branch session (Ctrl+K B): a new worktree on a new branch cut from the
 * focused session, carrying its uncommitted changes.
 *
 *  - the chord is a table row (Ctrl+K B), listed beside Duplicate everywhere
 *  - git only: on a folder outside git the row menu shows it greyed with the
 *    reason as its note, and the chord raises that reason as an error card
 *    (never a native alert — the desktop app has none)
 *  - left out (not explained) for a shared folder, another device's row, and
 *    a session still starting
 *  - POST /branch; a refusal is an error card with the server's words, a
 *    success lands under its source, takes focus and says what came along */

/// <reference types="vite/client" />
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Caps, Instance } from "../api/types";

const toast = vi.fn();
const errorPop = vi.fn();
const instApi = vi.fn();
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
vi.mock("../lib/errorPop", () => ({
  errorPop: (...a: unknown[]) => errorPop(...a),
  confirmPop: vi.fn(),
}));
vi.mock("../lib/terminals", () => ({ focusTerm: vi.fn(), releaseTerms: vi.fn() }));
vi.mock("../api/client", async (orig) => ({
  ...((await orig()) as object),
  instApi: (...a: unknown[]) => instApi(...a),
}));

const { CHORDS } = await import("../lib/keymap");
const { SESSION_ACTIONS, CHORD_ACTIONS, actionCtx, actionHint, runChordAction, sessionAction } = await import(
  "../lib/sessionActionTable"
);
const { sessionMenu } = await import("../lib/sessionMenu");
const { branchSession } = await import("../lib/sessionActions");
const { queryClient } = await import("../state/queries");
const { useUi } = await import("../state/store");
const { ApiError } = await import("../api/client");

const PALETTE_SRC = Object.values(
  import.meta.glob<string>("../components/palette/CommandPalette.tsx", {
    query: "?raw",
    import: "default",
    eager: true,
  })
)[0];

const CAPS: Caps = { git: true, tailscale: true, ticketing: true, github: true };
const row = (o: Partial<Instance> = {}) =>
  ({ title: "s", status: "ready", branch: "me/s", folder: "/w/s", git_repo: true, ...o }) as Instance;
const ctx = (o: Partial<Instance> = {}, caps: Caps = CAPS) =>
  actionCtx(o.title || "s", { inst: row(o), caps, ideName: "Cursor", hidden: false });
const menuItems = (o: Partial<Instance> = {}) => {
  const inst = row(o);
  const m = sessionMenu(inst, actionCtx(inst.title, { inst, caps: CAPS }));
  return [...m.groups.flatMap((g) => g.items), ...m.more];
};

afterEach(() => {
  toast.mockReset();
  errorPop.mockReset();
  instApi.mockReset();
  queryClient.setQueryData(["instances"], []);
  useUi.getState().setOrder([]);
});

describe("the Ctrl+K B chord", () => {
  it("is the table's Branch row, right after Duplicate", () => {
    expect(SESSION_ACTIONS.branch).toMatchObject({ label: "Branch session", keys: "Ctrl+K B", chord: "b" });
    expect(CHORD_ACTIONS.indexOf("branch")).toBe(CHORD_ACTIONS.indexOf("duplicate") + 1);
    expect(CHORDS.b.desc).toBe("Branch session");
    // Nobody else had B.
    expect(CHORD_ACTIONS.filter((id) => sessionAction(id).chord === "b")).toEqual(["branch"]);
  });

  it("says what it does in the palette hint", () => {
    expect(actionHint("branch", ctx())).toBe("Ctrl+K B · new worktree on a new branch from this one");
  });

  it("is listed in the palette and the row menu", () => {
    expect(PALETTE_SRC).toMatch(/"duplicate",\s*"branch",/);
    const item = menuItems().find((i) => i.key === "branch");
    expect(item).toBeTruthy();
    expect(item!.disabled).toBeFalsy();
    expect(item!.note).toBe("new worktree on a new branch from this one");
  });
});

describe("git only", () => {
  const plain = { folder: "/home/me/notes", in_place: true, branch: "", git_repo: false };

  it("is disabled outside git, with the reason as its note", () => {
    const c = ctx(plain);
    expect(SESSION_ACTIONS.branch.enabled(c)).toBe(false);
    expect(SESSION_ACTIONS.branch.why!(c)).toBe("Branch needs a git repository — /home/me/notes isn’t one");
    const item = menuItems(plain).find((i) => i.key === "branch")!;
    expect(item.disabled).toBe(true);
    expect(item.note).toBe("Branch needs a git repository — /home/me/notes isn’t one");
    item.run();
    expect(instApi).not.toHaveBeenCalled();
    expect(actionHint("branch", c)).toBe("Ctrl+K B · Branch needs a git repository — /home/me/notes isn’t one");
  });

  it("the chord on a folder outside git raises the reason as an error card", () => {
    queryClient.setQueryData(["instances"], [row({ title: "notes", ...plain })]);
    runChordAction("branch", "notes");
    expect(instApi).not.toHaveBeenCalled();
    expect(errorPop).toHaveBeenCalledWith(
      "Branch session isn’t available for notes",
      "Branch needs a git repository — /home/me/notes isn’t one"
    );
    expect(toast).not.toHaveBeenCalled();
  });

  it("without git installed it says so", () => {
    const c = ctx({}, { ...CAPS, git: false });
    expect(SESSION_ACTIONS.branch.enabled(c)).toBe(false);
    expect(SESSION_ACTIONS.branch.why!(c)).toContain("git is not installed");
  });

  it("an older server (no git_repo) leaves it on — the server still refuses", () => {
    expect(SESSION_ACTIONS.branch.enabled(ctx({ git_repo: undefined }))).toBe(true);
  });
});

describe("left out where it never applies", () => {
  it.each([
    ["a shared folder", { peer_share: true }],
    ["another device's row", { title: "rig::s" }],
    ["a session still starting", { status: "loading" }],
    ["a pending create", { pending: true } as Partial<Instance>],
  ])("%s", (_name, o) => {
    const c = ctx(o as Partial<Instance>);
    expect(SESSION_ACTIONS.branch.enabled(c)).toBe(false);
    expect(SESSION_ACTIONS.branch.why!(c)).toBe("");
    expect(menuItems(o as Partial<Instance>).find((i) => i.key === "branch")).toBeUndefined();
  });
});

describe("branchSession", () => {
  it("POSTs /branch, takes focus and says the changes came along", async () => {
    queryClient.setQueryData(["instances"], [row({ title: "api" })]);
    instApi.mockResolvedValue({ title: "api-branch", carried: true, branched_from: "api" });
    await branchSession("api");
    expect(instApi).toHaveBeenCalledWith("api", "/branch", { method: "POST" });
    expect(useUi.getState().focused).toBe("api-branch");
    const order = useUi.getState().order;
    expect(order.indexOf("api-branch")).toBe(order.indexOf("api") + 1);
    expect(toast).toHaveBeenCalledWith("Branched api → api-branch with its uncommitted changes", { duration: 4000 });
    expect(errorPop).not.toHaveBeenCalled();
  });

  it("a clean source says nothing about changes", async () => {
    queryClient.setQueryData(["instances"], [row({ title: "api" })]);
    instApi.mockResolvedValue({ title: "api-branch-2", carried: false });
    await branchSession("api");
    expect(toast).toHaveBeenCalledWith("Branched api → api-branch-2", { duration: 4000 });
    // The optimistic guess's slot is given back.
    expect(useUi.getState().order).not.toContain("api-branch");
  });

  it("a refusal is an error card with the server's words, and no row is left behind", async () => {
    queryClient.setQueryData(["instances"], [row({ title: "api" })]);
    instApi.mockRejectedValue(new ApiError(409, "Branch needs a git repository — /x isn’t one", {}));
    await branchSession("api");
    expect(errorPop).toHaveBeenCalledWith("Branch failed", expect.stringContaining("Branch needs a git repository"));
    const titles = (queryClient.getQueryData<Instance[]>(["instances"]) || []).map((i) => i.title);
    expect(titles).toEqual(["api"]);
    expect(useUi.getState().order).not.toContain("api-branch");
  });
});
