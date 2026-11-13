import { describe, expect, it } from "vitest";
import { pendingTitleFor } from "../components/dialogs/newSession";
import { mergePending } from "../state/queries";
import type { Instance } from "../api/types";

/** The optimistic row a create shows while it is answered is named as the
 * server will name the session — not "untitled" for every blank Name, which
 * flashed an "untitled" row in the sidebar that the real one then replaced
 * (and, on the friend's failed Windows create, one that came and went). */
describe("the pending row's name", () => {
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

  it("is untitled only with nothing to name it from", () => {
    expect(pendingTitleFor({ title: "", prompt: "", repo_path: "" })).toBe("untitled");
    expect(pendingTitleFor({})).toBe("untitled");
  });

  it("the flights block's create (folder + no task) names it from a C:\\ folder", () => {
    // NewSessionBlock passes { title: workerName, prompt, repo_path: folder }.
    expect(pendingTitleFor({ title: "", prompt: "", repo_path: "C:\\work\\api-server" })).toBe("api-server");
    expect(pendingTitleFor({ title: "orch-w2", prompt: "x", repo_path: "C:\\work\\api" })).toBe("orch-w2");
  });

  it("the server's row takes the pending row's place — no second, no 'untitled' left over", () => {
    const now = 1_000_000;
    const guess = pendingTitleFor({ title: "", prompt: "make me snake", repo_path: "C:\\Users\\maldo" });
    const pending = {
      title: guess,
      status: "loading",
      pending_create: true,
      pending_at: now,
      pending_base: guess,
    } as unknown as Instance;
    // The server numbered it (a closed make-me-snake still holds the name).
    const fetched = [{ title: "make-me-snake-2", status: "loading" } as Instance];
    const rows = mergePending(fetched, [pending], now + 500);
    expect(rows.map((r) => r.title)).toEqual(["make-me-snake-2"]);
  });
});
