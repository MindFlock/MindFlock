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

  it("never sums a section whose sizes are still being measured", () => {
    const sized = item({ free_bytes: 1024 ** 3 });
    const pending = item({ id: "p", size_bytes: null, free_bytes: null });
    expect(lib.groupFreesText([sized])).toBe("frees 1.0 GB");
    expect(lib.groupFreesText([sized, pending])).toBe("frees …");
    // A folder that is already gone has no size and doesn't hold it up.
    expect(lib.groupFreesText([sized, item({ ...pending, state: "missing" })])).toBe("frees 1.0 GB");
    expect(lib.summaryLine({ count: 2, bytes: null, safe_count: 1, safe_bytes: null })).toBe(
      "MindFlock made 2 of these: …. 1 can go safely, freeing ….",
    );
  });

  it("says what a running scan is doing", () => {
    const rep = (o: Partial<import("../lib/cleanup").CleanupReport>) =>
      ({ items: [], scanning: true, settings: { mode: "ask", after_days: 14 }, ...o }) as import("../lib/cleanup").CleanupReport;
    expect(lib.scanNote(rep({ scanning: false }))).toBe("");
    expect(lib.scanNote(rep({ items: null }))).toBe("");
    expect(lib.scanNote(rep({}))).toBe(" Checking again…");
    expect(lib.scanNote(rep({ sizing: true }))).toBe(" Measuring sizes…");
    expect(lib.scanNote(rep({ sizing: true, progress: { phase: "size", done: 3, total: 40 } }))).toBe(
      " Measuring sizes… 3/40",
    );
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

const facts = (o: Partial<import("../lib/cleanup").InspectFacts> = {}): import("../lib/cleanup").InspectFacts => ({
  path: "/h/.mindflock/worktrees/u/gone_1",
  name: "u/gone_1",
  files: 120,
  dirs: 9,
  bytes: 5 * 1024 ** 2,
  newest: 1_700_000_000,
  newest_file: "notes.md",
  work_files: 3,
  work_bytes: 2048,
  dependency_files: 117,
  symlinks: 0,
  mounts: [],
  complete: true,
  top: [{ name: "node_modules", dir: true, kind: "dependency", files: 117, bytes: 5 * 1024 ** 2 }],
  top_more: 0,
  work_sample: [{ path: "notes.md", bytes: 12, mtime: 1_700_000_000 }],
  git: {
    dot: "file",
    gitdir: "/r/.git/worktrees/gone_1",
    gitdir_exists: false,
    repo: "/r",
    repo_exists: true,
    branch: "u/gone",
    compare: { changed_count: 1, missing_count: 0, extra_count: 1, changed: ["a.py"], missing: [], extra: ["notes.md"] },
    notes: [],
  },
  verdict: "differs",
  safe: false,
  summary: ["120 files in 9 folders (5.0 MB).", "2 files differ from branch u/gone: 1 changed, 1 only here, 0 missing here."],
  ...o,
});
const gone = item({
  id: "g",
  path: "/h/.mindflock/worktrees/u/gone_1",
  name: "u/gone_1",
  kind: "orphan",
  state: "unchecked",
  safe: false,
  branch: "",
  size_bytes: 6 * 1024 ** 2,
  kept: ["git no longer lists it as a worktree of /r, so its files can't be checked"],
  unchecked_why: "unregistered",
});

describe("folders that can't be checked", () => {
  it("get their own section, never a plain Remove, and count in the summary", () => {
    expect(lib.STATE_LABEL.unchecked).toBe("Can't be checked");
    expect(lib.canRemove(gone)).toBe(false);
    const groups = lib.groupItems([base, gone]);
    expect(groups.map((g) => g.key)).toEqual(["safe", "unchecked"]);
    expect(groups[1].title).toContain("never removed automatically");
    expect(
      lib.summaryLine({ count: 2, bytes: 1, safe_count: 0, safe_bytes: 0, unchecked_count: 1, unchecked_bytes: 1024 }),
    ).toContain("1 can't be checked (1.0 KB): look inside each to decide.");
  });

  it("confirms in two steps naming the folder, its size and its work files", () => {
    const one = lib.uncheckedConfirm(gone, facts(), 1);
    expect(one.title).toBe("Delete u/gone_1 (6.0 MB) for good?");
    expect(one.body).toContain("120 files (5.0 MB), 3 of them look like work");
    expect(one.body).toContain("permanent");
    const two = lib.uncheckedConfirm(gone, facts(), 2);
    expect(two.title).toContain("permanently");
    expect(two.body).toContain("3 files that look like work and 6.0 MB");
    expect(two.confirmLabel).toBe("Delete permanently");
  });

  it("says a permanent deletion as one", () => {
    expect(lib.removedText(gone, { ok: true, permanent: true, deleted: { files: 120, work_files: 3, bytes: 1024 } })).toBe(
      "Deleted u/gone_1 permanently (120 files, 1.0 KB).",
    );
  });
});

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

  it("shows the list while the sizes are still being measured", () => {
    const html = render({
      rep: {
        items: [item({ size_bytes: null, free_bytes: null })],
        summary: { count: 1, bytes: null, safe_count: 1, safe_bytes: null },
        scanning: true,
        sizing: true,
        progress: { phase: "size", done: 0, total: 1 },
        settings: { mode: "ask", after_days: 14 },
      },
    });
    expect(html).toContain("u/done_1");
    expect(html).toContain("Measuring sizes… 0/1");
    expect(html).toContain("frees …");
    expect(html).not.toContain("frees 0 B");
  });

  it("shows the app's own confirm card for an item holding work", () => {
    const wip = item({ id: "b", name: "u/wip", state: "holds_work", safe: false, kept: ["1 untracked file"], untracked_files: 1 });
    const html = render({ asking: wip });
    expect(html).toContain('role="alertdialog"');
    expect(html).toContain("Remove u/wip?");
    expect(html).toContain("Remove anyway");
    expect(html).toContain("/h/.mindflock/kept-work");
  });

  it("offers Look inside for a folder that can't be checked, and no Remove", () => {
    const html = render({
      rep: {
        items: [gone],
        summary: { count: 1, bytes: 1, safe_count: 0, safe_bytes: 0, unchecked_count: 1, unchecked_bytes: 1 },
        scanning: false,
        settings: { mode: "ask", after_days: 14 },
      },
    });
    expect(html).toContain("Can&#x27;t be checked (never removed automatically)");
    expect(html).toContain("Look inside");
    expect(html).not.toContain(">Remove<");
  });

  it("shows what is inside and Remove anyway…, then each confirm step in the page", () => {
    const rep = {
      items: [gone],
      summary: { count: 1, bytes: 1, safe_count: 0, safe_bytes: 0 },
      scanning: false,
      settings: { mode: "ask" as const, after_days: 14 },
    };
    let html = render({ rep, looks: { g: facts() } });
    expect(html).toContain("120 files in 9 folders");
    expect(html).toContain("node_modules/");
    expect(html).toContain("notes.md");
    expect(html).toContain("Changed: a.py");
    expect(html).toContain("Remove anyway…");
    expect(html).not.toContain('role="alertdialog"');
    html = render({ rep, looks: { g: facts() }, deleting: { id: "g", step: 1 } });
    expect(html).toContain('role="alertdialog"');
    expect(html).toContain("Delete u/gone_1 (6.0 MB) for good?");
    expect(html).toContain("Continue…");
    html = render({ rep, looks: { g: facts() }, deleting: { id: "g", step: 2 } });
    expect(html).toContain("Delete permanently");
    expect(html).toContain("Keep it");
  });

  it("offers a plain Remove when the look found nothing that could be lost", () => {
    const rep = {
      items: [gone],
      summary: { count: 1, bytes: 1, safe_count: 0, safe_bytes: 0 },
      scanning: false,
      settings: { mode: "ask" as const, after_days: 14 },
    };
    const html = render({ rep, looks: { g: facts({ safe: true, verdict: "matches" }) } });
    expect(html).toContain(">Remove<");
    expect(html).not.toContain("Remove anyway…");
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
