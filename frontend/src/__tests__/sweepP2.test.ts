/** UI sweep, package P2 (sidebar, dialogs, palette, confirmations):
 *
 *  - Delete of an in-place session says its folder and branch were KEPT, in
 *    the toast and in the row menu's tooltip;
 *  - one Escape in the bell closes the bell only (the bulk bar's Escape sees
 *    it handled), focus goes into the panel on open and back to the bell;
 *  - Rename: the dialog stops at MAX_TITLE_LEN like the inline box, and the
 *    inline box stays open on a taken name, with the text and the reason;
 *  - bulk Close is on the Ctrl+Shift+T stack and its toast's Undo reopens
 *    every one of them;
 *  - the palette has View PR, Reopen last closed, Copy folder path;
 *  - Undo is a button on both Hide toasts and on a deleted saved prompt;
 *    Recently closed's Reopen says "Reopened X"; the template delete question
 *    says Cancel and focuses it. */

/// <reference types="vite/client" />
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Instance } from "../api/types";
import { code } from "./sourceText";

const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});

const toast = vi.fn();
const errorPop = vi.fn();
const instApi = vi.fn();
const api = vi.fn();
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
vi.mock("../lib/errorPop", () => ({
  errorPop: (...a: unknown[]) => errorPop(...a),
  confirmPop: vi.fn(),
}));
vi.mock("../api/client", async (orig) => ({
  ...((await orig()) as object),
  instApi: (...a: unknown[]) => instApi(...a),
  api: (...a: unknown[]) => api(...a),
}));

const { useUi } = await import("../state/store");
const { queryClient } = await import("../state/queries");
const SA = await import("../lib/sessionActions");
const { refocusBellOnEscape, BELL_FIRST_ITEM } = await import("../components/NotificationsBell");
const { inlineRenameVerdict, renameTakenText } = await import("../components/sidebar/SidebarRow");
const { bulkEndText } = await import("../components/sidebar/BulkBar");
const { restorePreset } = await import("../components/dialogs/PromptsDialog");
const { reopenedText } = await import("../components/dialogs/RecentDialog");
const { buildPaletteActions, copyPathOf } = await import("../components/palette/CommandPalette");

const rows = (list: Array<Partial<Instance>>) =>
  queryClient.setQueryData<Instance[]>(["instances"], list as Instance[]);

beforeEach(() => {
  toast.mockReset();
  errorPop.mockReset();
  instApi.mockReset();
  api.mockReset();
  useUi.setState({ aliases: {}, focused: null, hidden: new Set(), ending: {} } as never);
});

describe("#11 deleting an in-place session keeps its folder and branch", () => {
  it("says so in the toast", async () => {
    rows([{ title: "here", in_place: true }]);
    instApi.mockResolvedValue({});
    api.mockResolvedValue([]);
    await SA.deleteSession("here", true);
    expect(toast.mock.calls[0][0]).toBe("Deleted here — its folder and branch were kept");
  });

  it("a worktree session still says they were removed", async () => {
    rows([{ title: "wt" }]);
    instApi.mockResolvedValue({});
    api.mockResolvedValue([]);
    await SA.deleteSession("wt", true);
    expect(toast.mock.calls[0][0]).toBe("Deleted wt — worktree and branch removed");
  });

  it("the toast words and the menu tooltip pick by in_place", () => {
    expect(SA.deletedToastText("a", true, "1 worker session kept running")).toBe(
      "Deleted a — its folder and branch were kept · 1 worker session kept running"
    );
    expect(SA.deleteMenuTitle({ in_place: true })).toBe("Removes it from MindFlock; its folder and branch stay (asks first)");
    expect(SA.deleteMenuTitle({})).toBe("Deletes its folder and branch, uncommitted changes included (asks first)");
    expect(code("lib/sessionMenu.ts")).toContain("title: deleteMenuTitle(inst),");
  });
});

describe("#15 the bell's Escape", () => {
  it("is handled (preventDefault) so the bulk bar keeps select mode, and gives focus back", () => {
    const bell = code("components/NotificationsBell.tsx");
    const esc = bell.slice(bell.indexOf("const onKey = (e: KeyboardEvent) =>"));
    const body = esc.slice(0, esc.indexOf("document.addEventListener"));
    expect(body).toContain('if (e.key !== "Escape" || e.defaultPrevented) return;');
    expect(body).toContain("e.preventDefault();");
    expect(body).toContain("btnRef.current?.focus()");
    // The bulk bar's Escape stands down for a handled key.
    expect(code("components/sidebar/BulkBar.tsx")).toContain('e.key !== "Escape" || e.defaultPrevented');
  });

  it("refocuses the bell when focus was in the panel, on the bell or nowhere — not from a terminal", () => {
    const inside = { id: "row" } as unknown as Element;
    const term = { id: "xterm" } as unknown as Element;
    const btn = { id: "notif-btn" } as unknown as Element;
    const bodyEl = { id: "body" } as unknown as Element;
    const pop = { contains: (n: Node | null) => n === (inside as unknown as Node) };
    expect(refocusBellOnEscape(inside, pop, btn, bodyEl)).toBe(true);
    expect(refocusBellOnEscape(btn, pop, btn, bodyEl)).toBe(true);
    expect(refocusBellOnEscape(bodyEl, pop, btn, bodyEl)).toBe(true);
    expect(refocusBellOnEscape(null, pop, btn, bodyEl)).toBe(true);
    expect(refocusBellOnEscape(term, pop, btn, bodyEl)).toBe(false);
  });

  it("focuses the panel's first row when it opens", () => {
    expect(BELL_FIRST_ITEM).toContain(".notif-list .notif-item");
    const bell = code("components/NotificationsBell.tsx");
    expect(bell).toContain("popRef.current?.querySelector<HTMLElement>(BELL_FIRST_ITEM)?.focus(");
  });
});

describe("#21 rename: one length, one rule", () => {
  it("the Rename dialog stops at MAX_TITLE_LEN", () => {
    const dlg = code("components/dialogs/RenameDialog.tsx");
    const input = dlg.slice(dlg.indexOf('id="rename-input"'));
    expect(input.slice(0, input.indexOf("/>"))).toContain("maxLength={MAX_TITLE_LEN}");
  });

  it("the inline box refuses a taken name and stays open with it", () => {
    const taken = (n: string) => (n === "b" ? "b" : "");
    expect(inlineRenameVerdict("  b ", "a", taken)).toEqual({ kind: "taken", name: "b" });
    expect(inlineRenameVerdict("c", "a", taken)).toEqual({ kind: "rename", name: "c" });
    expect(inlineRenameVerdict("  ", "a", taken)).toEqual({ kind: "keep" });
    expect(inlineRenameVerdict("a", "a", taken)).toEqual({ kind: "keep" });
    expect(renameTakenText("b")).toBe("Another session is already named “b”.");
    const row = code("components/sidebar/SidebarRow.tsx");
    const commit = row.slice(row.indexOf("const commitRename"), row.indexOf("const rowCls"));
    // A taken name sets the reason and returns BEFORE the edit closes.
    const takenAt = commit.indexOf('if (verdict.kind === "taken")');
    expect(takenAt).toBeGreaterThan(0);
    expect(commit.indexOf("setEditing(false)", takenAt)).toBeGreaterThan(commit.indexOf("return;", takenAt));
    expect(row).toContain('className="lineage rename-taken"');
    expect(row).not.toContain("toast(`Another session is already named");
  });
});

describe("#25 bulk Close can be undone", () => {
  it("records each closed title for Ctrl+Shift+T", async () => {
    SA.rememberClosed("one");
    api.mockResolvedValueOnce([
      { id: "1", title: "one", closed_at: 5, exists: true },
      { id: "0", title: "older", closed_at: 9, exists: true },
    ]);
    api.mockResolvedValueOnce({ title: "one" });
    api.mockResolvedValue([]);
    await SA.undoLastClose();
    // The remembered one, though "older" closed later.
    expect(api.mock.calls[1][0]).toBe("/api/recently-closed/1/reopen");
  });

  it("the Undo reopens every one of them, newest entry each", async () => {
    api.mockImplementation(async (path: string) => {
      if (path === "/api/recently-closed")
        return [
          { id: "a1", title: "a", closed_at: 1, exists: true },
          { id: "a2", title: "a", closed_at: 2, exists: true },
          { id: "b1", title: "b", closed_at: 1, exists: true },
          { id: "c1", title: "c", closed_at: 1, exists: false },
        ];
      if (path.endsWith("/reopen")) return { title: path.split("/")[3] };
      return [];
    });
    await SA.reopenClosed(["a", "b", "c"]);
    const reopened = api.mock.calls.map((c) => c[0]).filter((p: string) => p.endsWith("/reopen"));
    expect(reopened.sort()).toEqual(["/api/recently-closed/a2/reopen", "/api/recently-closed/b1/reopen"]);
    expect(toast).toHaveBeenCalledWith("Reopened 2 sessions · 1 could not be reopened", { duration: 5000 });
    expect(errorPop.mock.calls[0][1]).toContain("c — its worktree is gone");
  });

  it("the bulk toast carries the Undo, and bulk Close records what it closed", () => {
    expect(bulkEndText("Closed", 2, 3, null)).toBe("Closed 2/3 session(s)");
    expect(SA.reopenedManyText(1, 0)).toBe("Reopened 1 session");
    const bar = code("components/sidebar/BulkBar.tsx");
    expect(bar).toContain("for (const t of done) rememberClosed(t);");
    expect(bar).toContain("onClick: () => void reopenClosed(closed)");
    // The question is unchanged.
    expect(bar).toContain("`Close ${n} session(s)? Keeps their worktrees — reopen from Recently closed.`");
  });
});

describe("#27 the palette has the row menu's missing verbs", () => {
  it("View PR, Reopen last closed and Copy folder path", () => {
    rows([{ title: "s", folder: "/w/s", pr_url: "https://example.test/pr/1", status: "running" } as Partial<Instance>]);
    useUi.setState({ focused: "s", order: ["s"] } as never);
    const acts = buildPaletteActions({
      config: { caps: { git: true, tailscale: true, ticketing: true, github: true } } as never,
      host: {} as never,
      extensions: [],
    });
    const labels = acts.map((a) => a.label);
    expect(labels).toContain("View PR ↗ — s");
    expect(labels).toContain("Reopen last closed session");
    expect(labels).toContain("Copy folder path — s");
    expect(acts.find((a) => a.label === "Reopen last closed session")?.hint).toBe("Ctrl+Shift+T");
    expect(copyPathOf({ path: "/p" })).toBe("/p");
    expect(copyPathOf(undefined)).toBe("");
  });
});

describe("#33 undo and confirm wording", () => {
  it("Hide's toast has an Undo button that unhides", () => {
    SA.hideSession("h");
    expect(useUi.getState().hidden.has("h")).toBe(true);
    const [msg, opts] = toast.mock.calls[0];
    expect(msg).toBe("Hid h");
    expect(opts.action.label).toBe("Undo");
    expect(opts.onClick).toBeUndefined();
    opts.action.onClick();
    expect(useUi.getState().hidden.has("h")).toBe(false);
    const bar = code("components/sidebar/BulkBar.tsx");
    expect(bar).toContain("toast(`Hid ${hid.length} session(s)`, {");
    expect(bar).not.toContain("click to undo");
  });

  it("a deleted saved prompt can be undone, back where it was", () => {
    const a = { name: "a", prompt: "1" };
    const b = { name: "b", prompt: "2" };
    const c = { name: "c", prompt: "3" };
    expect(restorePreset([a, c], b, 1)).toEqual([a, b, c]);
    expect(restorePreset([a, c], b, 9)).toEqual([a, c, b]);
    // Saved again since: left as it is.
    expect(restorePreset([a, b], { name: "b", prompt: "new" }, 0)).toEqual([a, b]);
    const dlg = code("components/dialogs/PromptsDialog.tsx");
    expect(dlg).toContain('toast("Deleted “" + p.name + "”", {');
    expect(dlg).toContain("const back = restorePreset(loadUserPresets(), gone, at);");
  });

  it("Recently closed's Reopen says what it reopened", () => {
    useUi.setState({ aliases: { x: "Login fix" } } as never);
    expect(reopenedText("x")).toBe("Reopened Login fix");
    expect(reopenedText("")).toBe("Reopened session");
    expect(code("components/dialogs/RecentDialog.tsx")).toContain('toast(reopenedText(inst?.title || e.title || ""));');
  });

  it("the template delete question says Cancel and focuses it", () => {
    const tpl = code("components/dialogs/TemplatesDialog.tsx");
    const q = tpl.slice(tpl.indexOf('className="tpl-confirm"'));
    const no = q.slice(q.indexOf("tpl-confirm-no"));
    expect(no.slice(0, no.indexOf("</button>"))).toMatch(/autoFocus[\s\S]*Cancel/);
    expect(q.slice(0, q.indexOf("</div>"))).not.toContain(">Keep<");
  });
});
