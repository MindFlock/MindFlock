/** A push the remote branch has moved past (409 `diverged`) raises an inline
 * card — "Rebase onto <remote>/<branch> and push" or Cancel — never a
 * force-push; the rebase goes through POST /rebase-remote and then pushes. */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { ApiError } from "../api/client";

const instApi = vi.fn();

vi.mock("../api/client", async (orig) => {
  const actual = (await orig()) as Record<string, unknown>;
  return {
    ...actual,
    api: vi.fn(async () => ({})),
    instApi: (...args: unknown[]) => instApi(...args),
  };
});

vi.mock("../state/queries", async (orig) => {
  const actual = (await orig()) as Record<string, unknown>;
  return { ...actual, refreshInstances: vi.fn(async () => {}), patchInstance: vi.fn() };
});

const errorPop = vi.fn();
const confirmPop = vi.fn();
vi.mock("../lib/errorPop", () => ({
  errorPop: (...args: unknown[]) => errorPop(...args),
  confirmPop: (...args: unknown[]) => confirmPop(...args),
}));

const { pushSession, divergedPush } = await import("../lib/sessionActions");

type Action = { label: string; run: () => void };

const DIVERGED = {
  error: "feat moved on origin: 2 new commits by Alice",
  diverged: true,
  behind: 2,
  authors: ["Alice"],
  remote: "origin",
  branch: "feat",
};

describe("the diverged push card", () => {
  beforeEach(() => {
    instApi.mockReset();
    errorPop.mockReset();
    confirmPop.mockReset();
  });

  it("reads only a 409 that says diverged", () => {
    expect(divergedPush(new ApiError(409, DIVERGED.error, DIVERGED))).toEqual({
      error: DIVERGED.error,
      remote: "origin",
      branch: "feat",
      behind: 2,
      authors: ["Alice"],
    });
    expect(divergedPush(new ApiError(409, "checks haven't passed for this commit", { error: "x" }))).toBeNull();
    expect(divergedPush(new ApiError(500, "x", DIVERGED))).toBeNull();
    expect(divergedPush(new Error("x"))).toBeNull();
  });

  it("offers Rebase-and-push or Cancel, then rebases and pushes again", async () => {
    let pushes = 0;
    instApi.mockImplementation(async (_t: string, path: string) => {
      if (path === "/push-branch" && pushes++ === 0) throw new ApiError(409, DIVERGED.error, DIVERGED);
      return {};
    });
    await pushSession("pair");
    expect(errorPop).not.toHaveBeenCalled();
    expect(confirmPop).toHaveBeenCalledTimes(1);
    const [title, detail, actions, cancel] = confirmPop.mock.calls[0] as [string, string, Action[], string];
    expect(title).toBe("Push blocked — origin/feat moved on");
    expect(detail).toContain("pair");
    expect(detail).toContain("2 new commits by Alice");
    expect(actions.map((a) => a.label)).toEqual(["Rebase onto origin/feat and push"]);
    expect(cancel).toBe("Cancel");

    instApi.mockClear();
    actions[0].run();
    await vi.waitFor(() => expect(instApi.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(instApi.mock.calls[0][1]).toBe("/rebase-remote");
    expect(instApi.mock.calls[0][2]).toEqual({ json: { remote: "origin" } });
    expect(instApi.mock.calls[1][1]).toBe("/push-branch");
    // never a force-push from this path
    expect(instApi.mock.calls[1][2]).toEqual({ json: {} });
  });

  it("a conflicting rebase stops with the server's word and does not push", async () => {
    instApi.mockImplementation(async (_t: string, path: string) => {
      if (path === "/push-branch") throw new ApiError(409, DIVERGED.error, DIVERGED);
      if (path === "/rebase-remote")
        throw new ApiError(409, "rebasing onto origin/feat conflicts in f.txt — the rebase was aborted", {
          conflicts: ["f.txt"],
        });
      return {};
    });
    await pushSession("pair");
    const actions = confirmPop.mock.calls[0][2] as Action[];
    instApi.mockClear();
    actions[0].run();
    await vi.waitFor(() => expect(errorPop).toHaveBeenCalled());
    expect(errorPop.mock.calls[0][0]).toBe("Rebase stopped — nothing changed");
    expect(errorPop.mock.calls[0][1]).toContain("f.txt");
    expect(instApi.mock.calls.map((c) => c[1])).toEqual(["/rebase-remote"]);
  });
});

import ROW_SRC from "../components/sidebar/SidebarRow.tsx?raw";
import PANE_SRC from "../components/grid/Pane.tsx?raw";

describe("shared branch on the rail and the pane head", () => {
  it("the row marks a shared-branch session beside its presence dots", () => {
    const at = ROW_SRC.indexOf("<PresenceDots inst={inst} me={me} />");
    const mark = ROW_SRC.indexOf("{inst.shared_branch && (");
    expect(mark).toBeGreaterThan(at);
    expect(mark - at).toBeLessThan(120);
    expect(ROW_SRC).toContain('className="shared-branch-mark"');
  });

  it("the pane head shows how far behind the remote it is, only when > 0, never as a stagechip", () => {
    expect(PANE_SRC).toContain("{(inst.behind || 0) > 0 && (");
    const chip = PANE_SRC.slice(PANE_SRC.indexOf("{(inst.behind || 0) > 0 && ("), PANE_SRC.indexOf("{hasDiffStat && <CtxLine"));
    expect(chip).toContain('className="pane-behind"');
    expect(chip).not.toMatch(/className="[^"]*stagechip/);
  });
});
