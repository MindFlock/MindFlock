/** The day-to-day session loop: re-selecting a row, the "needs you" chips as
 * doors, the rail's keyboard, Ctrl+K N and the palette's needs-you-first menu,
 * Open PR / Merge saying what they did. Pure pieces are called; wiring is
 * pinned in the module that owns it (sourceText.ts). */

import { describe, expect, it, vi } from "vitest";
import type { Instance, OutboxWaiting } from "../api/types";
import { deriveSessionState, needsYouAction } from "../lib/sessionState";
import { code, fn, has } from "./sourceText";

vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { railRowKey } = await import("../components/sidebar/SidebarRow");
const { mergedText } = await import("../lib/sessionActions");
const { needsYouTitles, nextNeedsYou } = await import("../lib/needsYou");
const { rankActions, NEEDS_LIMIT, TOP_LIMIT } = await import("../components/palette/paletteRank");
const { usableShortcuts } = await import("../components/palette/ShortcutsSheet");

const inst = (title: string, extra: Partial<Instance> = {}) =>
  ({ title, status: "running", activity: "idle", stage: "agent", branch: "", ...extra }) as Instance;

describe("a click on the selected row re-selects it", () => {
  it("never arms a rename; the name's double-click, F2 and the menu do", () => {
    const row = code("components/sidebar/SidebarRow.tsx");
    expect(row).not.toContain("armRename");
    expect(row).not.toContain("renameTimer");
    expect(row).not.toContain("Click again to rename");
    // The row click ends in reselect(), selected or not…
    expect(has(fn("components/sidebar/SidebarRow.tsx", "SidebarRow"), "reselect();")).toBe(true);
    // …which focuses the terminal and closes a narrow window's drawer.
    expect(has(row, "selectSession(title); const ui = useUi.getState(); if (ui.narrow && !ui.sidebarHidden) ui.toggleSidebar();")).toBe(true);
    expect(has(row, 'if (k === "rename") { e.preventDefault(); startRename(); return; }')).toBe(true);
  });
});

describe("the rail's keyboard", () => {
  it("↑/↓/Home/End move, Enter selects, F2 renames — only unmodified", () => {
    expect(railRowKey({ key: "ArrowUp" })).toBe("up");
    expect(railRowKey({ key: "ArrowDown" })).toBe("down");
    expect(railRowKey({ key: "Home" })).toBe("first");
    expect(railRowKey({ key: "End" })).toBe("last");
    expect(railRowKey({ key: "Enter" })).toBe("select");
    expect(railRowKey({ key: "F2" })).toBe("rename");
    expect(railRowKey({ key: "ArrowDown", shiftKey: true })).toBeNull();
    expect(railRowKey({ key: "Enter", ctrlKey: true })).toBeNull();
    expect(railRowKey({ key: "1" })).toBeNull();
  });
});

describe("needs-you chips are doors", () => {
  const ss = (o: Partial<Instance>) => deriveSessionState(inst("s", o), { now: Date.now() });

  it("each reason goes where you act on it", () => {
    expect(needsYouAction(ss({ activity: "clarify" }))?.act).toBe("bell");
    expect(needsYouAction(ss({ stage: "interrupt" }))?.act).toBe("terminal");
    expect(needsYouAction(ss({ setup: { state: "failed" } } as Partial<Instance>))?.act).toBe("setup");
    const checks = ss({ check: { state: "failed", rc: 1 } } as Partial<Instance>);
    expect(needsYouAction(checks, "https://github.com/o/r/pull/3")?.act).toBe("pr");
    expect(needsYouAction(checks, null)?.act).toBe("terminal");
  });

  it("every other state is just a word", () => {
    for (const a of ["idle", "working", "limit", "offline"]) expect(needsYouAction(ss({ activity: a })), a).toBeNull();
  });

  it("re-running setup is a session action the chip and the menu share in spirit", () => {
    expect(has(fn("lib/sessionActions.ts", "rerunSetup"), 'instApi(title, "/setup/rerun", { method: "POST" })')).toBe(true);
  });
});

describe("Open PR and Merge say what they did", () => {
  it("Open PR goes through offerUrl — a bare window.open after an await is popup-blocked", () => {
    const make = fn("lib/sessionActions.ts", "submitMakePr");
    expect(make).not.toMatch(/window\.open\(r\.url/);
    expect(has(make, "if (r?.url) offerUrl(r.url, said);")).toBe(true);
    expect(has(make, '(r?.note ? "PR already open for " : "Opened PR for ") + displayName(title)')).toBe(true);
  });

  it("a merge names where it landed", () => {
    expect(mergedText("fix-typo", "main")).toBe("Merged fix-typo into main");
    expect(mergedText("fix-typo", "")).toBe("Merged fix-typo");
    expect(has(fn("lib/sessionActions.ts", "mergeSession"), "toast(mergedText(displayName(title), r?.base));")).toBe(true);
  });
});

describe("Ctrl+K N: the next session that needs you", () => {
  it("walks the bell's order: an answer before a broken session before failing checks", () => {
    const rows = [
      inst("checks", { check: { state: "failed", rc: 1 } } as Partial<Instance>),
      inst("broken", { stage: "interrupt" }),
      inst("asks", { activity: "clarify" }),
      inst("calm"),
    ];
    const order = needsYouTitles(rows, []);
    expect(order).toEqual(["asks", "broken", "checks"]);
    expect(nextNeedsYou(order, null)).toBe("asks");
    expect(nextNeedsYou(order, "calm")).toBe("asks");
    expect(nextNeedsYou(order, "asks")).toBe("broken");
    expect(nextNeedsYou(order, "checks")).toBe("asks"); // wraps
    expect(nextNeedsYou([], "asks")).toBeNull();
  });

  it("takes an approval from the outbox in, and leaves a group row with no session out", () => {
    const waiting = [
      { title: "approve-me", kind: "approve" },
      { title: "", kind: "budget", key: "run::r1", run: { id: "r1" } },
    ] as unknown as OutboxWaiting[];
    const order = needsYouTitles([inst("approve-me"), inst("asks", { activity: "clarify" })], waiting);
    expect(order).toEqual(["asks", "approve-me"]);
  });

  it("is a chord that needs no focused session, listed under Navigation", () => {
    const km = code("lib/keymap.ts");
    expect(has(km, "if (chord.global) { chord.run(focused); return; }")).toBe(true);
    expect(has(km, "run: (t) => jumpToNeedsYou(t)")).toBe(true);
    expect(has(code("components/palette/ShortcutsSheet.tsx"), 'rows[CHORDS[k].group || "Focused session"].push([')).toBe(true);
  });
});

describe("the palette", () => {
  const a = (label: string, top?: number) => ({ label, top, run() {} });
  const allRow = (n: number) => a("All commands… " + n);

  it("leads an empty query with what needs you, ahead of the usual menu", () => {
    const acts = [a("New session…", 0), a("Open Intake", 2), a("Focus: asks", -1), a("Focus: broken", -0.99)];
    const out = rankActions(acts, "", { allRow }).map((x) => x.label);
    expect(out.slice(0, 4)).toEqual(["Focus: asks", "Focus: broken", "New session…", "Open Intake"]);
  });

  it("caps the needs-you rows and still shows the full usual menu", () => {
    const urgent = Array.from({ length: 8 }, (_, i) => a("Focus: n" + i, -1 + i / 100));
    const usual = Array.from({ length: 12 }, (_, i) => a("cmd" + i, i));
    const out = rankActions([...usual, ...urgent], "", { allRow });
    expect(out.filter((x) => x.label.startsWith("Focus:"))).toHaveLength(NEEDS_LIMIT);
    expect(out.filter((x) => x.label.startsWith("cmd"))).toHaveLength(TOP_LIMIT);
  });

  it("names sessions as the rail does, shows each one's state, and is a listbox", () => {
    const p = code("components/palette/CommandPalette.tsx");
    expect(p).not.toContain("${t}`");
    expect(has(p, 'label: "Focus: " + windowName(inst.title), state: { label: ss.label, tone: ss.tone }')).toBe(true);
    expect(has(p, 'top: need >= 0 ? NEEDS_TOP + need / 100')).toBe(true);
    expect(p).toContain('role="combobox"');
    expect(p).toContain('aria-activedescendant={filtered.length ? "palette-opt-" + sel : undefined}');
    expect(p).toContain('role="listbox"');
    expect(p).toContain('role="option"');
    expect(p).toContain("aria-selected={i === sel}");
    expect(has(p, '?.scrollIntoView?.({ block: "nearest" });')).toBe(true);
    // Find file on map… opens the focused session's Map through the store.
    expect(has(p, "run: () => useUi.getState().openMap(t)")).toBe(true);
  });
});

describe("shortcut hints say keys that work here", () => {
  it("a browser tab gets Alt+N, the desktop app both", () => {
    expect(usableShortcuts("new", true)).toEqual(["Alt+N"]);
    expect(usableShortcuts("new", false)).toEqual(["Ctrl+N", "Alt+N"]);
    const grid = code("components/grid/TerminalGrid.tsx");
    expect(grid).not.toContain("(Ctrl+N)");
    expect(grid).toContain('usableShortcuts("new").join(" / ")');
  });
});

describe("the bell, for a screen reader and a keyboard", () => {
  it("says the count its badge shows", async () => {
    const { bellLabel } = await import("../components/NotificationsBell");
    expect(bellLabel(3, 0)).toBe("Notifications, 3 need you");
    expect(bellLabel(1, 4)).toBe("Notifications, 1 needs you");
    expect(bellLabel(0, 2)).toBe("Notifications, 2 new");
    expect(bellLabel(0, 0)).toBe("Notifications");
  });

  it("its click-target rows are keyboard stops; a question's row keeps the strip's buttons", () => {
    const bell = code("components/NotificationsBell.tsx");
    expect(bell).toContain("aria-label={bellLabel(attn.length, unread)}");
    expect(bell).toContain("aria-expanded={open}");
    expect(has(bell, 'role={a.p === 0 ? undefined : "button"} tabIndex={a.p === 0 ? undefined : 0}')).toBe(true);
    expect(has(bell, 'role="button" tabIndex={0} onKeyDown={rowKey}')).toBe(true);
    expect(has(fn("components/NotificationsBell.tsx", "rowKey"), "if (e.target !== e.currentTarget")).toBe(true);
  });
});

describe("a device group's header", () => {
  it("is a button with aria-expanded, and its ✕ asks before forgetting the token", () => {
    const head = fn("components/sidebar/Sidebar.tsx", "DeviceHeader");
    expect(has(head, 'className="dev-toggle" aria-expanded={!props.collapsed} onClick={props.onToggle}')).toBe(true);
    expect(head).not.toMatch(/<li className="device-group"[^>]*onClick/);
    // The ✕ only arms the strip; the POST is the strip's Disconnect.
    expect(has(head, "setArmed(!armed);")).toBe(true);
    expect(has(head, 'className="danger" onClick={() => void disconnect()}')).toBe(true);
    expect(head.indexOf("/disconnect")).toBeLessThan(head.indexOf("<li"));
  });
});

describe("the Queue tab", () => {
  it("Ctrl/⌘+Enter sends now, Alt+Enter queues, Enter is a newline", async () => {
    const { composerKey } = await import("../components/grid/QueueTab");
    expect(composerKey({ key: "Enter", ctrlKey: true })).toBe("send");
    expect(composerKey({ key: "Enter", metaKey: true })).toBe("send");
    expect(composerKey({ key: "Enter", altKey: true })).toBe("add");
    expect(composerKey({ key: "Enter" })).toBeNull();
    expect(composerKey({ key: "Enter", shiftKey: true, ctrlKey: true })).toBeNull();
    expect(composerKey({ key: "Enter", ctrlKey: true, altKey: true })).toBeNull();
  });

  it("names the session as the rail does, labels its icon buttons, and undoes a delete", () => {
    const q = code("components/grid/QueueTab.tsx");
    expect(q).toContain('<span className="queue-pop-title">{displayName(title)}</span>');
    for (const l of ['"Send prompt "', '"Edit prompt "', '"Add a prompt above prompt "', '"Add a prompt below prompt "', '"Remove prompt "'])
      expect(q, l).toContain("aria-label={" + l);
    const rm = fn("components/grid/QueueTab.tsx", "QueueTab");
    expect(has(rm, 'onClick: () => void mutate("/queue", { json: { text: it.text, index } })')).toBe(true);
    expect(has(rm, 'toast("Removed from the queue", {')).toBe(true);
  });
});
