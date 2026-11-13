import { beforeEach, describe, expect, it, vi } from "vitest";

const toasts: string[] = [];
vi.mock("../lib/toast", async (orig) => ({
  ...(await orig<typeof import("../lib/toast")>()),
  toast: (msg: string) => {
    toasts.push(msg);
  },
}));

import { pendingTitleFor } from "../components/dialogs/newSession";
import { failPendingSession, landPendingSession, startPendingCreate } from "../lib/sessionActions";
import { mergePending, notePendingCreated, queryClient } from "../state/queries";
import type { Instance } from "../api/types";

/** The owner's rule: a row called "untitled" NEVER appears in the sidebar
 * during a create. The optimistic row is named as the server will name it,
 * becomes the real row in place, and — with nothing to name it from — is
 * not added at all (a "Creating…" toast stands in). */

const rows = () => (queryClient.getQueryData<Instance[]>(["instances"]) || []).map((r) => r.title);
const set = (titles: string[]) =>
  queryClient.setQueryData<Instance[]>(["instances"], titles.map((t) => ({ title: t }) as unknown as Instance));

beforeEach(() => {
  toasts.length = 0;
  set(["other"]);
});

describe("the pending row's name (pendingTitleFor)", () => {
  it("is the typed Name when there is one", () => {
    expect(pendingTitleFor({ title: " my-run ", prompt: "make me snake", repo_path: "/r/app" })).toBe("my-run");
  });

  it("with a blank Name, comes from the task, as the dialog's preview does", () => {
    // The friend's dialog: Name empty, task "make me snake", preview make-me-snake.
    expect(pendingTitleFor({ title: "", prompt: "make me snake", repo_path: "C:\\Users\\maldo" })).toBe("make-me-snake");
  });

  it("with no task either, comes from the folder — a Windows path too", () => {
    expect(pendingTitleFor({ title: "", prompt: "", repo_path: "C:\\Users\\maldo\\Snake Game\\" })).toBe("snake-game");
    expect(pendingTitleFor({ title: "", repo_path: "/home/me/sitecheck-bot7" })).toBe("sitecheck-bot7");
  });

  it("is empty — never “untitled” — with nothing to name it from", () => {
    expect(pendingTitleFor({ title: "", prompt: "", repo_path: "" })).toBe("");
    expect(pendingTitleFor({})).toBe("");
    expect(pendingTitleFor({ prompt: "!!!", repo_path: "/" })).toBe("");
  });

  it("the flights block's create (folder + no task) names it from a C:\\ folder", () => {
    // NewSessionBlock passes { title: workerName, prompt, repo_path: folder }.
    expect(pendingTitleFor({ title: "", prompt: "", repo_path: "C:\\work\\api-server" })).toBe("api-server");
    expect(pendingTitleFor({ title: "orch-w2", prompt: "x", repo_path: "C:\\work\\api" })).toBe("orch-w2");
  });
});

describe("(1) the pending row becomes the real row in place", () => {
  it("one row named make-me-snake at every step: press → answer → poll", () => {
    const guess = startPendingCreate(pendingTitleFor({ title: "", prompt: "make me snake", repo_path: "C:\\Users\\maldo" }));
    expect(guess).toBe("make-me-snake");
    expect(rows()).toEqual(["other", "make-me-snake"]);
    // The create answers: the row takes the answer, same place, same name.
    landPendingSession(guess, { title: "make-me-snake", status: "loading" } as Instance);
    expect(rows()).toEqual(["other", "make-me-snake"]);
    // The poll lists it: still exactly one.
    const cached = queryClient.getQueryData<Instance[]>(["instances"]);
    const polled = mergePending([{ title: "other" }, { title: "make-me-snake" }] as Instance[], cached, Date.now());
    expect(polled.map((r) => r.title)).toEqual(["other", "make-me-snake"]);
    // A poll that does not list it YET keeps it: no gap.
    const early = mergePending([{ title: "other" }] as Instance[], cached, Date.now());
    expect(early.map((r) => r.title)).toEqual(["other", "make-me-snake"]);
    expect(toasts).toEqual([]);
  });

  it("the session.created event before the answer does not add a second row", () => {
    const guess = startPendingCreate("make-me-snake");
    const evented = notePendingCreated(queryClient.getQueryData<Instance[]>(["instances"]) || [], "make-me-snake", {}, Date.now());
    queryClient.setQueryData(["instances"], evented);
    landPendingSession(guess, { title: "make-me-snake" } as Instance);
    expect(rows()).toEqual(["other", "make-me-snake"]);
  });

  it("a failed create takes its row away and leaves no “untitled” behind", () => {
    const guess = startPendingCreate("make-me-snake");
    failPendingSession(guess);
    expect(rows()).toEqual(["other"]);
  });
});

describe("(2) nothing to name it from: no row, a “Creating…” toast", () => {
  it("adds no pending row and says Creating…", () => {
    const guess = startPendingCreate(pendingTitleFor({ title: "", prompt: "", repo_path: "" }));
    expect(guess).toBe("");
    expect(rows()).toEqual(["other"]);
    expect(rows()).not.toContain("untitled");
    expect(toasts).toEqual(["Creating…"]);
  });

  it("the real row appears when the server answers (the poll), whatever it is called", () => {
    startPendingCreate("");
    const polled = mergePending(
      [{ title: "other" }, { title: "untitled" }] as Instance[],
      queryClient.getQueryData<Instance[]>(["instances"]),
      Date.now()
    );
    // The server's own session — not a placeholder of ours.
    expect(polled.map((r) => r.title)).toEqual(["other", "untitled"]);
  });
});

describe("(3) the server numbered it: the pending row is renamed in place", () => {
  it("make-me-snake → make-me-snake-2 in the same slot, never two rows", () => {
    set(["a", "b"]);
    const guess = startPendingCreate("make-me-snake");
    // A row arriving meanwhile lands after the pending one.
    queryClient.setQueryData<Instance[]>(["instances"], (prev) => [...(prev || []), { title: "c" } as Instance]);
    expect(rows()).toEqual(["a", "b", "make-me-snake", "c"]);
    landPendingSession(guess, { title: "make-me-snake-2" } as Instance);
    expect(rows()).toEqual(["a", "b", "make-me-snake-2", "c"]);
  });

  it("the poll after a numbered answer keeps one row (the server's)", () => {
    const guess = startPendingCreate("make-me-snake");
    landPendingSession(guess, { title: "make-me-snake-2" } as Instance);
    const polled = mergePending(
      [{ title: "other" }, { title: "make-me-snake-2" }] as Instance[],
      queryClient.getQueryData<Instance[]>(["instances"]),
      Date.now()
    );
    expect(polled.map((r) => r.title)).toEqual(["other", "make-me-snake-2"]);
  });

  it("a numbered title seen first by a poll also claims the pending row (no duplicate)", () => {
    startPendingCreate("make-me-snake");
    const polled = mergePending(
      [{ title: "other" }, { title: "make-me-snake-2" }] as Instance[],
      queryClient.getQueryData<Instance[]>(["instances"]),
      Date.now()
    );
    expect(polled.map((r) => r.title)).toEqual(["other", "make-me-snake-2"]);
  });
});
