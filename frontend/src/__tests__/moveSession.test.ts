/** Move to a folder… (docs/flights.md "Moving a session"):
 *
 *  - moveSession POSTs /move {dest, force}; a 202 marks the row "moving…" and
 *    toasts "Moving “name”…"; a 409 `working` without force is the dialog's
 *    to answer (no card); every other refusal is an error card, in the
 *    server's words or, for a bare code, ours
 *  - the "moving…" mark survives a poll and goes on `session.moved`,
 *    `session.move_failed`, or after a minute
 *  - `session.moved` refetches and toasts where it went; `session.move_failed`
 *    is an error card; replays only refetch
 *  - the dialog: the current folder, the field seeded with its parent + "/",
 *    the sentence, the mid-turn box, Cancel / Move — and no confirm/prompt */

/// <reference types="vite/client" />
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Instance } from "../api/types";
import { code } from "./sourceText";

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

const { QueryClientProvider } = await import("@tanstack/react-query");
const { ApiError } = await import("../api/client");
const { MOVE_MID_TURN, moveErrorText, moveRefusal, moveSession, openMoveSession } = await import("../lib/sessionActions");
const {
  MOVE_TTL_MS,
  bridgeSessionSetEvents,
  clearMoving,
  isMovingRow,
  markMoving,
  movedToastText,
  onSessionMoveFailed,
  onSessionMoved,
  queryClient,
  withMoving,
} = await import("../state/queries");
const { useUi } = await import("../state/store");
const { MoveSessionDialog, moveBlockReason, moveDestSeed, sessionFolder } = await import(
  "../components/dialogs/MoveSessionDialog"
);

// The instances query reads document.hidden for its poll cadence.
(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const row = (o: Partial<Instance> = {}) =>
  ({ title: "s", status: "ready", branch: "me/s", path: "/w/repo/s", folder: "/w/repo", ...o }) as Instance;
const rows = () => queryClient.getQueryData<Instance[]>(["instances"]) || [];
const env = (event: string, session: string, data: Record<string, unknown>) =>
  ({ seq: 1, event, session, old: null, new: null, ts: 0, data }) as Parameters<typeof onSessionMoved>[0];
const refusal = (status: number, body: unknown, msg = "MindFlock couldn't do that (error " + status + ").") =>
  new ApiError(status, msg, body);

beforeEach(() => {
  queryClient.setQueryData(["instances"], [row(), row({ title: "other", path: "/w/repo/other" })]);
});
afterEach(() => {
  toast.mockReset();
  errorPop.mockReset();
  instApi.mockReset();
  clearMoving("s");
  clearMoving("other");
  queryClient.setQueryData(["instances"], []);
  useUi.getState().closeDialog();
  vi.useRealTimers();
});

describe("moveSession — the request and what it says", () => {
  it("POSTs /move with dest and force; a 202 toasts and marks the row moving", async () => {
    instApi.mockResolvedValue({ title: "s", from: "/w/repo/s", to: "/w/new/s" });
    const r = await moveSession("s", "/w/new/s");
    expect(instApi).toHaveBeenCalledWith("s", "/move", { method: "POST", json: { dest: "/w/new/s", force: false } });
    expect(r).toEqual({ ok: true, to: "/w/new/s" });
    expect(toast).toHaveBeenCalledWith("Moving “s”…");
    expect(errorPop).not.toHaveBeenCalled();
    expect(rows().filter(isMovingRow).map((i) => i.title)).toEqual(["s"]);
  });

  it("names the session by its window name (a rename) in the toast", async () => {
    useUi.setState({ aliases: { ...useUi.getState().aliases, s: "Login fix" } } as never);
    instApi.mockResolvedValue({});
    const r = await moveSession("s", "/w/x", true);
    expect(instApi.mock.calls[0][2]).toEqual({ method: "POST", json: { dest: "/w/x", force: true } });
    expect(r).toEqual({ ok: true, to: "/w/x" });
    expect(toast).toHaveBeenCalledWith("Moving “Login fix”…");
    const { s: _drop, ...rest } = useUi.getState().aliases;
    useUi.setState({ aliases: rest } as never);
  });

  it("a 409 working without force: no card, the dialog answers it", async () => {
    instApi.mockRejectedValue(refusal(409, { error: "working" }, "working"));
    const r = await moveSession("s", "/w/x");
    expect(r).toEqual({ ok: false, refusal: "working", message: MOVE_MID_TURN + "." });
    expect(errorPop).not.toHaveBeenCalled();
    expect(toast).not.toHaveBeenCalled();
    expect(rows().some(isMovingRow)).toBe(false);
  });

  it("every other refusal is an error card, in the server's words when it sent some", async () => {
    instApi.mockRejectedValue(refusal(409, { error: "exists", detail: "/w/x already exists" }, "/w/x already exists"));
    const r = await moveSession("s", "/w/x");
    expect(r).toMatchObject({ ok: false, refusal: "exists" });
    expect(errorPop).toHaveBeenCalledWith("Moving “s” failed", "/w/x already exists");
    expect(rows().some(isMovingRow)).toBe(false);
  });

  it("a forced move the server still refuses as working goes on the card", async () => {
    instApi.mockRejectedValue(refusal(409, { error: "working" }, "working"));
    await moveSession("s", "/w/x", true);
    expect(errorPop).toHaveBeenCalledWith("Moving “s” failed", MOVE_MID_TURN + ".");
  });

  it("maps every named refusal; anything else is its own message", () => {
    for (const [status, code] of [
      [400, "bad_dest"],
      [409, "exists"],
      [409, "working"],
      [409, "in_flight"],
      [409, "remote"],
      [404, "unknown_session"],
    ] as const) {
      const err = refusal(status, { error: code }, code);
      expect(moveRefusal(err), code).toBe(code);
      // A bare code is never shown as is.
      expect(moveErrorText(err), code).not.toBe(code);
      expect(moveErrorText(err).length, code).toBeGreaterThan(20);
    }
    expect(moveRefusal(refusal(409, { code: "in_flight" }))).toBe("in_flight");
    expect(moveRefusal(refusal(500, { error: "boom" }, "boom"))).toBe("");
    expect(moveRefusal(new Error("offline"))).toBe("");
    expect(moveErrorText(new Error("offline"))).toBe("offline");
    expect(moveErrorText(refusal(400, { error: "bad_dest", detail: "relative path" }, "relative path"))).toBe("relative path");
  });

  it("a network failure is a card too", async () => {
    instApi.mockRejectedValue(new Error("Failed to fetch"));
    const r = await moveSession("s", "/w/x");
    expect(r).toEqual({ ok: false, refusal: "", message: "Failed to fetch" });
    expect(errorPop).toHaveBeenCalledWith("Moving “s” failed", "Failed to fetch");
  });

  it("openMoveSession opens the move dialog on the title", () => {
    openMoveSession("s");
    expect(useUi.getState().openDialog).toBe("move");
    expect(useUi.getState().dialogTarget).toBe("s");
  });
});

describe("the moving… mark", () => {
  it("survives a poll (withMoving) and only marks the moving row", () => {
    const since = new Map([["s", 1000]]);
    const out = withMoving([row(), row({ title: "other" })], 2000, since);
    expect(out.map(isMovingRow)).toEqual([true, false]);
    // Nothing pending: the rows as they are.
    const fetched = [row()];
    expect(withMoving(fetched, 2000, new Map())).toBe(fetched);
  });

  it("forgets a mark older than the TTL", () => {
    const since = new Map([["s", 0]]);
    expect(withMoving([row()], MOVE_TTL_MS + 1, since).some(isMovingRow)).toBe(false);
    expect(since.size).toBe(0);
  });

  it("markMoving marks the cache; clearMoving unmarks it", () => {
    markMoving("s");
    expect(rows().find((r) => r.title === "s")!).toMatchObject({ moving: true });
    clearMoving("s");
    expect(rows().some(isMovingRow)).toBe(false);
  });

  it("clears itself after a minute with no word from the server", () => {
    vi.useFakeTimers();
    markMoving("s", Date.now());
    vi.advanceTimersByTime(MOVE_TTL_MS - 1);
    expect(rows().some(isMovingRow)).toBe(true);
    vi.advanceTimersByTime(2);
    expect(rows().some(isMovingRow)).toBe(false);
  });
});

describe("the move's events", () => {
  it("session.moved: unmarks, refetches and toasts where it went", () => {
    markMoving("s");
    const inv = vi.spyOn(queryClient, "invalidateQueries");
    onSessionMoved(env("session.moved", "s", { title: "s", from: "/w/repo/s", to: "/w/new/s" }));
    expect(rows().some(isMovingRow)).toBe(false);
    expect(inv).toHaveBeenCalledWith({ queryKey: ["instances"] });
    expect(toast).toHaveBeenCalledWith("Moved “s” to /w/new/s", { duration: 5000 });
    inv.mockRestore();
  });

  it("reads the title from the payload when the envelope has none", () => {
    onSessionMoved(env("session.moved", "", { title: "s", to: "/w/new/s" }));
    expect(toast).toHaveBeenCalledWith("Moved “s” to /w/new/s", { duration: 5000 });
  });

  it("session.move_failed: unmarks and puts the reason on an error card", () => {
    markMoving("s");
    onSessionMoveFailed(env("session.move_failed", "s", { title: "s", error: "git worktree move: locked" }));
    expect(rows().some(isMovingRow)).toBe(false);
    expect(errorPop).toHaveBeenCalledWith("Moving “s” failed", "git worktree move: locked");
  });

  it("a replay only unmarks and refetches", () => {
    markMoving("s");
    onSessionMoved(env("session.moved", "s", { to: "/w/new/s" }), true);
    onSessionMoveFailed(env("session.move_failed", "s", { error: "x" }), true);
    expect(rows().some(isMovingRow)).toBe(false);
    expect(toast).not.toHaveBeenCalled();
    expect(errorPop).not.toHaveBeenCalled();
  });

  it("the toast's words", () => {
    expect(movedToastText("s", "/w/new/s")).toBe("Moved “s” to /w/new/s");
    expect(movedToastText("s", "")).toBe("Moved “s”");
  });

  it("both are subscribed once, beside session.create_failed", () => {
    const subs: Record<string, (e: unknown) => void> = {};
    const g = globalThis as Record<string, unknown>;
    const had = "window" in g;
    const prev = g.window;
    g.window = {
      mindflock: {
        events: { subscribe: (n: string, cb: (e: unknown) => void) => ((subs[n] = cb), () => {}), isReplay: () => false },
      },
    };
    try {
      bridgeSessionSetEvents();
    } finally {
      if (had) g.window = prev;
      else delete g.window;
    }
    expect(Object.keys(subs)).toEqual(expect.arrayContaining(["session.create_failed", "session.moved", "session.move_failed"]));
    subs["session.move_failed"](env("session.move_failed", "s", { error: "disk full" }));
    expect(errorPop).toHaveBeenCalledWith("Moving “s” failed", "disk full");
    const src = code("state/queries.ts");
    expect(src.indexOf('ev.subscribe("session.moved"')).toBeGreaterThan(src.indexOf('ev.subscribe("session.create_failed"'));
  });
});

describe("MoveSessionDialog", () => {
  const render = () => {
    // A server render reads the store's initial state (getServerSnapshot):
    // lend it the live one for this render only.
    const init = useUi.getInitialState();
    const saved = { ...init };
    Object.assign(init, useUi.getState());
    try {
      return renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(MoveSessionDialog)));
    } finally {
      Object.assign(init, saved);
    }
  };

  it("draws nothing unless it is open on a session", () => {
    expect(render()).toBe("");
    useUi.getState().openDialogFor("move", null);
    expect(render()).toBe("");
  });

  it("shows where it is, the field seeded with its parent, the sentence, the box and Cancel / Move", () => {
    useUi.getState().openDialogFor("move", "s");
    const html = render();
    expect(html).toContain('id="move-dialog"');
    expect(html).toContain("Move “s” to a folder");
    expect(html).toContain("Now in");
    expect(html).toContain("/w/repo/s");
    // The new folder's path: the parent + "/", so typing a name completes.
    expect(html).toMatch(/<input[^>]*id="move-dest"[^>]*value="\/w\/repo\/"/);
    expect(html).toContain('id="move-dest-browse"');
    expect(html).toContain("MindFlock pauses the agent, moves the folder, and resumes it there. Linked worktrees are repaired.");
    expect(html).toMatch(/<input type="checkbox" id="move-force"[^>]*\/>Even if it is mid-turn/);
    expect(html).toMatch(/id="move-cancel"[^>]*>Cancel</);
    // Seeded with the parent alone, Move waits for a name.
    expect(html).toMatch(/<button type="submit" id="move-submit" disabled="" title="Add the new folder&#x27;s name">Move</);
    // The mid-turn line shows only after a refusal.
    expect(html).not.toContain(MOVE_MID_TURN);
  });

  it("names the session by its window name", () => {
    useUi.setState({ aliases: { ...useUi.getState().aliases, other: "Docs pass" } } as never);
    useUi.getState().openDialogFor("move", "other");
    expect(render()).toContain("Move “Docs pass” to a folder");
    const { other: _drop, ...rest } = useUi.getState().aliases;
    useUi.setState({ aliases: rest } as never);
  });

  it("seeds and guards the path", () => {
    expect(sessionFolder(row())).toBe("/w/repo/s");
    expect(sessionFolder(row({ path: "" }))).toBe("/w/repo");
    expect(sessionFolder(undefined)).toBe("");
    expect(moveDestSeed("/w/repo/s")).toBe("/w/repo/");
    expect(moveDestSeed("/w/repo/s/")).toBe("/w/repo/");
    expect(moveDestSeed("C:\\w\\s")).toBe("C:\\w\\");
    expect(moveDestSeed("/s")).toBe("");
    expect(moveDestSeed("")).toBe("");
    expect(moveBlockReason("/w/repo/s", "")).toBe("Type the new folder's path");
    expect(moveBlockReason("/w/repo/s", "/w/repo/s/")).toBe("That is where it is now");
    expect(moveBlockReason("/w/repo/s", "/w/repo/")).toBe("Add the new folder's name");
    expect(moveBlockReason("/w/repo/s", "/w/repo/s2")).toBe("");
    expect(moveBlockReason("/w/repo/s", "/w/other/")).toBe("");
  });

  it("is mounted with the app's dialogs and asks nothing natively (Electron has no prompt)", () => {
    const app = code("App.tsx");
    expect(app).toContain('import { MoveSessionDialog } from "./components/dialogs/MoveSessionDialog";');
    expect(app).toMatch(/<RenameDialog \/>\s*<MoveSessionDialog \/>/);
    const src = code("components/dialogs/MoveSessionDialog.tsx");
    expect(src).not.toMatch(/\b(confirm|prompt|alert)\(/);
    expect(src).toContain("<PathField");
    expect(src).toContain("<Modal");
  });
});
