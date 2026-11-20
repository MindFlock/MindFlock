/** Settings → Disk cleanup (components/settings/screens/Cleanup.tsx +
 * lib/cleanup.ts): the words for why an item goes or stays, the confirm
 * card's consequence for anything holding work (never window.confirm — the
 * desktop app has none), the screen's sections and its one collect button,
 * and the wiring that makes it reachable (nav, palette, assistant, toast). */

import { describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import screenSrc from "../components/settings/screens/Cleanup.tsx?raw";
import navSrc from "../components/settings/SettingsDialog.tsx?raw";
import toastsSrc from "../components/EventToasts.tsx?raw";

vi.mock("../api/client", () => ({ api: vi.fn() }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const lib = await import("../lib/cleanup");
const { CleanupView } = await import("../components/settings/screens/Cleanup");
const { searchSettings } = await import("../components/palette/settingsSearch");
const { ASSISTANT_SETTINGS_SCREENS } = await import("../lib/assistantHost");
type Item = import("../lib/cleanup").CleanupItem;

const base: Item = {
  id: "a",
  path: "/h/.mindflock/worktrees/u/done_1",
  name: "u/done_1",
  kind: "worktree",
  repo: "/r",
  branch: "u/done",
  state: "safe",
  safe: true,
  ours: true,
  in_use: "",
  merged: "content",
  merged_into: "main",
  kept: [],
  size_bytes: 3 * 1024 ** 3,
  last_used: Date.now() / 1000 - 40 * 86400,
  session: "done",
  tracked_changes: 0,
  untracked_files: 0,
  unsaved_commits: 0,
};
const item = (o: Partial<Item>): Item => ({ ...base, ...o });

describe("the words", () => {
  it("says why a safe item goes", () => {
    expect(lib.whyLine(base)).toBe("Merged into main; nothing uncommitted.");
    expect(lib.whyLine(item({ merged: "pr" }))).toBe("Its PR merged; nothing uncommitted.");
    expect(lib.whyLine(item({ merged: "" }))).toContain("Its branch is kept");
  });

  it("lists every reason a kept item stays", () => {
    const it2 = item({ safe: false, state: "holds_work", kept: ["2 uncommitted changes", "used 3 days ago"] });
    expect(lib.whyLine(it2)).toBe("2 uncommitted changes; used 3 days ago.");
  });

  it("asks only for what holds work, and says where the work goes", () => {
    expect(lib.needsConfirm(base)).toBe(false);
    const wip = item({ safe: false, state: "holds_work", untracked_files: 1, kept: ["1 untracked file"] });
    expect(lib.needsConfirm(wip)).toBe(true);
    expect(lib.confirmText(wip, "/h/.mindflock/kept-work")).toContain("saved first as a patch in /h/.mindflock/kept-work");
    expect(lib.confirmText(item({ safe: false, unsaved_commits: 2 }))).toContain("branch u/done is kept");
    expect(lib.confirmText(item({ kind: "orphan", safe: false }))).toContain("can't be checked");
  });

  it("never offers Remove on something in use or locked", () => {
    expect(lib.canRemove(item({ in_use: "session 'x' (running)" }))).toBe(false);
    expect(lib.canRemove(item({ kept: ["locked in git"] }))).toBe(false);
    expect(lib.canRemove(item({ safe: false, kept: ["1 untracked file"] }))).toBe(true);
  });

  it("tells where removed work was saved", () => {
    const t = lib.removedText(base, {
      ok: true,
      saved: { dir: "/k/x", patch: "/k/x/changes.patch", bundle: "", kept_branch: "" },
      branch_deleted: "",
    });
    expect(t).toBe("Removed u/done_1. Uncommitted changes saved to /k/x/changes.patch.");
  });

  it("formats sizes and the summary", () => {
    expect(lib.humanBytes(3 * 1024 ** 3)).toBe("3.0 GB");
    expect(lib.humanBytes(null)).toBe("…");
    expect(lib.summaryLine({ count: 3, bytes: 4 * 1024 ** 3, safe_count: 1, safe_bytes: 3 * 1024 ** 3 })).toBe(
      "MindFlock made 3 of these: 4.0 GB. 1 can go safely, freeing 3.0 GB.",
    );
    // Worktrees MindFlock didn't make never inflate "mine" or "reclaimable".
    expect(
      lib.summaryLine({ count: 1, bytes: 1024, other_count: 82, other_bytes: 5 * 1024 ** 3, safe_count: 0, safe_bytes: 0 }),
    ).toBe(
      "MindFlock made 1 of these: 1.0 KB. Nothing is safe to remove right now. 82 other worktrees of the same repos (5.0 GB) are listed, never removed.",
    );
    expect(lib.summaryLine({ count: 1, bytes: 10, safe_count: 0, safe_bytes: 0 })).toContain("Nothing is safe");
  });

  it("tells on-disk size from what a removal frees (hard-linked caches)", () => {
    expect(lib.sizeText(item({ size_bytes: 10 * 1024 ** 3, free_bytes: 1.7 * 1024 ** 3 }))).toBe(
      "10 GB on disk · frees 1.7 GB",
    );
    expect(lib.sizeText(item({ size_bytes: 1024 ** 3, free_bytes: 1024 ** 3 }))).toBe("1.0 GB");
    expect(lib.freeBytes(item({ free_bytes: undefined, size_bytes: 5 }))).toBe(5);
  });

  it("defaults the mode to Ask me", () => {
    expect(lib.cleanupModeOf(undefined)).toBe("ask");
    expect(lib.cleanupModeOf("auto")).toBe("auto");
    expect(lib.CLEANUP_MODES.map((m) => m.label)).toEqual(["Automatic", "Ask me", "Off"]);
  });
});

function render(over: Partial<Parameters<typeof CleanupView>[0]> = {}) {
  const noop = () => {};
  return renderToStaticMarkup(
    createElement(CleanupView, {
      rep: {
        items: [
          base,
          item({ id: "b", name: "u/wip", state: "holds_work", safe: false, kept: ["1 untracked file"], untracked_files: 1 }),
          item({ id: "c", name: "u/live", state: "in_use", safe: false, in_use: "session 'live' (running)", kept: ["session 'live' (running)"] }),
        ],
        summary: { count: 3, bytes: 9 * 1024 ** 3, safe_count: 1, safe_bytes: 3 * 1024 ** 3 },
        scanning: false,
        settings: { mode: "ask", after_days: 14 },
        kept_dir: "/h/.mindflock/kept-work",
      },
      busy: "",
      asking: null,
      days: "14",
      onDays: noop,
      onSaveDays: noop,
      onMode: noop,
      onRefresh: noop,
      onCollect: noop,
      onRemove: noop,
      onCancel: noop,
      ...over,
    }),
  );
}

describe("the screen", () => {
  it("draws the summary, one collect button and the sections", () => {
    const html = render();
    expect(html).toContain("Disk cleanup");
    expect(html).toContain("Remove 1 safe (frees 3.0 GB)");
    expect(html).toContain("Will be removed");
    expect(html).toContain("Kept");
    expect(html).toContain("In use");
    expect(html).toContain(`checked="" value="ask"`);
  });

  it("shows the app's own confirm card for an item holding work", () => {
    const wip = item({ id: "b", name: "u/wip", state: "holds_work", safe: false, kept: ["1 untracked file"], untracked_files: 1 });
    const html = render({ asking: wip });
    expect(html).toContain('role="alertdialog"');
    expect(html).toContain("Remove u/wip?");
    expect(html).toContain("Remove anyway");
    expect(html).toContain("/h/.mindflock/kept-work");
  });

  it("before the first scan says it is looking", () => {
    const html = render({ rep: { items: null, scanning: true, settings: { mode: "ask", after_days: 14 } } });
    expect(html).toContain("Looking at your disk");
    expect(html).toContain("Nothing safe to remove");
  });

  it("uses no browser dialog", () => {
    expect(screenSrc).not.toMatch(/window\.(confirm|alert|prompt)\(|\bconfirm\(/);
  });
});

describe("the wiring", () => {
  it("is a Settings screen, found by search and by the assistant", () => {
    expect(navSrc).toContain(`{ key: "cleanup", label: "Disk cleanup", group: "Code", advanced: true`);
    expect(searchSettings("disk space").some((e) => e.key === "cleanup")).toBe(true);
    expect(searchSettings("old worktrees").some((e) => e.key === "cleanup")).toBe(true);
    expect(ASSISTANT_SETTINGS_SCREENS.cleanup).toBe("Disk cleanup");
  });

  it("toasts the server's one-time notice with a way in", () => {
    expect(toastsSrc).toContain('ev.subscribe("system.cleanup_available"');
    expect(toastsSrc).toContain('openSettings("cleanup")');
    expect(lib.cleanupNoticeText({ detail: "12 leftover worktrees (31 GB) can be removed safely." })).toContain("31 GB");
  });
});
