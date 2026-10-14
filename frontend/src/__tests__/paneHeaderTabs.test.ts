/** The pane header after "drop the pause button, bring Terminal and Map back,
 * and copy the entire channel" (owner, 2026-10-14):
 *
 *  - Terminal and Map are in EVERY header. They used to fold into ⋯ whenever
 *    the header was "compact" — and compact was `calm || paneNarrow`, so the
 *    calm first hour (a newcomer with < 3 sessions) hid them even in a
 *    1500px-wide pane. A narrow head draws the tabs as icons instead.
 *  - No ❚❚ Pause button and no Pause in the ⋯ menus; ▶ Resume only on a
 *    paused session. The palette, `mindflock pause` and the API still pause.
 *  - A copy-whole-history button in the header (and ⋯ → Copy entire history),
 *    sharing the History overlay's loader (lib/paneHistory). */

import { afterEach, describe, expect, it, vi } from "vitest";
import { code, CSS } from "./sourceText";
import type { Instance } from "../api/types";

const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});

const toast = vi.fn();
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
const copyText = vi.fn();
vi.mock("../lib/clipboard", () => ({ copyText: (t: string) => copyText(t) }));
vi.mock("../lib/terminals", () => ({ peekTerm: () => undefined }));

const { sessionMenu } = await import("../lib/sessionMenu");
const { actionCtx, SESSION_ACTIONS } = await import("../lib/sessionActionTable");
const { copyPaneHistory, fetchPaneHistory } = await import("../lib/paneHistory");

const caps = { git: true, tailscale: true, ticketing: true, github: true };
const menu = (o: Partial<Instance> = {}, opts = {}) => {
  const inst = { title: "s", status: "ready", branch: "me/b", folder: "/w/s", ...o } as Instance;
  return sessionMenu(inst, actionCtx("s", { inst, caps }), opts);
};
const allKeys = (m: ReturnType<typeof menu>) => [...m.groups.flatMap((g) => g.items.map((i) => i.key)), ...m.more.map((i) => i.key)];

const pane = code("components/grid/Pane.tsx");

describe("Terminal and Map are in every pane header", () => {
  it("no longer hang off `compact` (calm first hour or a narrow pane)", () => {
    expect(pane).toContain("const showShellTab = !sharedFolder;");
    expect(pane).toContain("const showMapTab = caps.git;");
    expect(pane).not.toMatch(/showShellTab = [^;]*compact/);
    expect(pane).not.toMatch(/showMapTab = [^;]*compact/);
    // The pane hands both to its ⋯ as visible, so the menu doesn't repeat them.
    expect(pane).toContain('const visibleTabs = [showShellTab && "shell", showMapTab && "map"]');
  });

  it("every tab carries an icon and its word; a narrow head shows the icon", () => {
    for (const t of ["agent", "shell", "diff", "map", "queue", "thread"]) expect(pane).toContain(`<TabIcon name="${t}" />`);
    for (const w of ["Agent", "Terminal", "Diff", "Map", "Queue", "Thread"])
      expect(pane).toContain(`<span className="tab-label">${w}</span>`);
    const css = CSS["components/grid/Pane.css"];
    expect(css).toMatch(/\.pane-head \.tabs \.tab-icon \{\s*display: none;/);
    const narrow = css.slice(css.indexOf("@container (max-width: 760px)"));
    expect(narrow).toMatch(/^@container \(max-width: 760px\) \{\s*\.pane-head \.tabs \.tab-icon \{\s*display: block;/);
    // The word stays for screen readers (visually hidden, not display:none).
    expect(narrow.slice(0, narrow.indexOf("\n}\n"))).toMatch(/\.tab-label \{\s*position: absolute;[\s\S]*clip: rect\(0 0 0 0\);/);
  });

  it("the pane ⋯ menu leaves Terminal / Code map to the header; the row menu keeps them", () => {
    const paneMenu = allKeys(menu({}, { visibleTabs: ["shell", "map"] }));
    expect(paneMenu).not.toContain("terminal");
    expect(paneMenu).not.toContain("map");
    const rowMenu = allKeys(menu());
    expect(rowMenu).toContain("terminal");
    expect(rowMenu).toContain("map");
  });
});

describe("no Pause button; Resume only when paused", () => {
  it("the header has no Pause, and ▶ Resume only when Resume applies", () => {
    expect(pane).not.toContain("pane-pause");
    expect(pane).not.toContain('"Pause session"');
    expect(pane).toContain('const canResume = sessionAction("resume").enabled(menuCtx);');
    expect(pane).toMatch(/\{canResume && \(\s*<button\s+className="act pane-resume"/);
    expect(pane).toContain('sessionAction("resume").run(title);');
    // The paused placeholder (with its own Resume) is still the Agent tab.
    expect(pane).toContain('<PausedBody key="paused" title={title} hidden={tab !== "agent"} />');
  });

  it("neither ⋯ menu offers Pause; a paused session gets Resume", () => {
    expect(allKeys(menu())).not.toContain("pause");
    expect(allKeys(menu({}, { visibleTabs: ["shell", "map"] }))).not.toContain("pause");
    expect(allKeys(menu())).not.toContain("resume");
    expect(allKeys(menu({ status: "paused" }))).toContain("resume");
    expect(allKeys(menu({ status: "paused" }))).not.toContain("pause");
    expect(allKeys(menu({ peer_share: true } as Partial<Instance>))).not.toContain("pause");
  });

  it("pausing still exists: the table, the palette", () => {
    expect(SESSION_ACTIONS.pause.label).toBe("Pause session");
    const pal = code("components/palette/CommandPalette.tsx");
    const list = pal.slice(pal.indexOf("const PALETTE_ACTIONS"), pal.indexOf("];", pal.indexOf("const PALETTE_ACTIONS")));
    expect(list).toContain('"pause"');
    expect(list).toContain('"resume"');
  });
});

describe("copy the entire history", () => {
  afterEach(() => {
    toast.mockReset();
    copyText.mockReset();
    vi.unstubAllGlobals();
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: () => {}, removeItem: () => {}, clear: () => {} });
  });
  const serve = (status: number, body: string) => {
    const fetch = vi.fn(async (_url: string) => new Response(body, { status }));
    vi.stubGlobal("fetch", fetch);
    return fetch;
  };

  it("a header button copies the ACTIVE terminal tab, disabled on tabs with no terminal", () => {
    expect(pane).toContain('const termTab: "agent" | "shell" | null = tab === "agent" || tab === "shell" ? tab : null;');
    expect(pane).toContain("if (termTab) void copyPaneHistory(title, termTab);");
    const btn = pane.slice(pane.indexOf('className="act copy-history"'), pane.indexOf("</button>", pane.indexOf('className="act copy-history"')));
    expect(btn).toContain("disabled={!termTab}");
    expect(btn).toContain('aria-label="Copy this pane\'s whole history"');
    expect(btn).toContain("copyHistory();");
    // Not folded by the calm/narrow header: no `!compact &&` in front of it.
    const before = pane.slice(pane.indexOf('className="act copy-history"') - 300, pane.indexOf('className="act copy-history"'));
    expect(before).not.toMatch(/!compact && \(\s*<button\s*$/);
  });

  it("the pane ⋯ offers Copy entire history only on a terminal tab", () => {
    const run = vi.fn();
    const m = menu({}, { visibleTabs: ["shell", "map"], onCopyHistory: run });
    const item = m.more.find((i) => i.key === "copy-history")!;
    expect(item.label).toBe("Copy entire history");
    item.run();
    expect(run).toHaveBeenCalledOnce();
    expect(allKeys(menu({}, { visibleTabs: ["shell", "map"] }))).not.toContain("copy-history");
    expect(pane).toContain("onCopyHistory: termTab ? copyHistory : undefined,");
  });

  it("one loader: the overlay's Copy all and the header share lib/paneHistory", () => {
    const ov = code("components/grid/HistoryOverlay.tsx");
    expect(ov).toContain("const fetchHistory = fetchPaneHistory;");
    expect(ov).toContain("const copyAll = () => void copyPaneHistory(title, pane, text);");
    expect(ov).not.toContain("/history?pane=");
    expect(pane).not.toContain("/history?pane=");
  });

  it("fetches the pane's history and toasts what it copied", async () => {
    const fetch = serve(200, "one\ntwo\n");
    copyText.mockResolvedValue(true);
    expect(await copyPaneHistory("a b", "shell")).toBe(true);
    expect(fetch.mock.calls[0][0]).toBe("/api/instances/a%20b/history?pane=shell");
    expect(copyText).toHaveBeenCalledWith("one\ntwo\n");
    expect(toast).toHaveBeenCalledWith("Copied full shell history (8 chars)");
  });

  it("says when there is nothing, when the clipboard refused, and when the fetch failed", async () => {
    serve(200, "  \n");
    expect(await copyPaneHistory("s", "agent")).toBe(false);
    expect(toast).toHaveBeenLastCalledWith("No history to copy");
    expect(copyText).not.toHaveBeenCalled();

    serve(200, "x");
    copyText.mockResolvedValue(false);
    expect(await copyPaneHistory("s", "agent")).toBe(false);
    expect(toast).toHaveBeenLastCalledWith("Copy failed");

    serve(500, "boom");
    expect(await copyPaneHistory("s", "agent")).toBe(false);
    expect(String(toast.mock.lastCall?.[0])).toMatch(/^History copy failed: /);
  });

  it("uses history already in hand instead of fetching it again", async () => {
    const fetch = serve(200, "fresh");
    copyText.mockResolvedValue(true);
    await copyPaneHistory("s", "agent", "loaded");
    expect(fetch).not.toHaveBeenCalled();
    expect(copyText).toHaveBeenCalledWith("loaded");
    expect(await fetchPaneHistory("s", "agent")).toBe("fresh");
  });
});
