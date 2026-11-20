/** UI sweep round 3, package R3: the sidebar's bars, filter and run groups,
 * and the "?" sheet.
 *
 * - `/` brings back a hidden filter box (no check for the box in its `when`).
 * - The sidebar filter matches each typed word, rows and open windows alike.
 * - The "?" sheet takes focus, its rebindable rows are keyboard buttons,
 *   Restore defaults has an Undo, ↺ says what it did, Talk shows Alt+Space.
 * - The GitHub PRs / issues bars match the Tickets bar (switch label,
 *   aria-label, red dot) and Customize uses their names.
 * - A team-run header folds from the keyboard; its ⋯ menu takes ↑ / ↓.
 * - The guest bar rests while hidden; a failed Kill is an error card. */

import { afterEach, describe, expect, it } from "vitest";
import type { Instance } from "../api/types";
import { has, normCode, fn } from "./sourceText";
import {
  CHORDS,
  KEYMAP,
  getKeyOverride,
  installKeymap,
  keyOverridesSnapshot,
  resetAllOverrides,
  restoreKeyOverrides,
  setChordKey,
  setKeyCombos,
  chordKeyFor,
} from "../lib/keymap";
import { matchesFilter, matchesWindowFilter } from "../components/sidebar/ordering";
import { SIDEBAR_BARS } from "../components/sidebar/barDefs";
import { stepMenu } from "../components/sidebar/RunGroupHeader";

afterEach(() => resetAllOverrides());

const g = globalThis as Record<string, unknown>;

/** Run `body` with a stand-in document whose focus is `active`. */
function withDoc<T>(active: unknown, body: () => T): T {
  const had = "document" in g;
  const prev = g.document;
  g.document = {
    activeElement: active,
    getElementById: () => null,
    addEventListener() {},
    removeEventListener() {},
  };
  try {
    return body();
  } finally {
    if (had) g.document = prev;
    else delete g.document;
  }
}

const el = (tagName: string, inMap = false) => ({
  tagName,
  isContentEditable: false,
  closest: (sel: string) => (inMap && sel === ".cm-root" ? {} : null),
});

describe("/ brings back a hidden filter box", () => {
  const slash = KEYMAP.find((e) => e.id === "filter")!;

  it("fires with no #sidebar-search on the page, so the host can summon it", () => {
    expect(withDoc(el("BODY"), () => slash.when!())).toBe(true);
    const calls: string[] = [];
    const host = new Proxy({} as Parameters<typeof installKeymap>[0], {
      get: (_t, name) => () => void calls.push(String(name)),
    });
    withDoc(el("BODY"), () => {
      const off = installKeymap(host);
      try {
        slash.run({} as KeyboardEvent);
      } finally {
        off();
      }
    });
    expect(calls).toEqual(["focusFilter"]);
  });

  it("still stays out of text fields and a Map tab's own find box", () => {
    expect(withDoc(el("INPUT"), () => slash.when!())).toBe(false);
    expect(withDoc(el("DIV", true), () => slash.when!())).toBe(false);
    expect(normCode("lib/keymap.ts")).not.toContain('getElementById("sidebar-search")');
  });
});

describe("the sidebar filter matches each word", () => {
  const row = { title: "fix-login-bug", branch: "me/fix-login-bug" } as unknown as Instance;

  it("finds fix-login-bug from “fix bug”, in any order and case", () => {
    expect(matchesFilter(row, "fix bug", {})).toBe(true);
    expect(matchesFilter(row, "  BUG   fix ", {})).toBe(true);
    expect(matchesFilter(row, "fix crash", {})).toBe(false);
    expect(matchesFilter(row, "", {})).toBe(true);
    // An alias's words count too.
    expect(matchesFilter(row, "auth flow", { "fix-login-bug": "Auth flow" })).toBe(true);
  });

  it("matches an open window's title the same way", () => {
    expect(matchesWindowFilter("System logs", "logs sys")).toBe(true);
    expect(matchesWindowFilter("System logs", "logs chat")).toBe(false);
    expect(matchesWindowFilter("System logs", "  ")).toBe(true);
    expect(has(normCode("components/sidebar/Sidebar.tsx"), "matchesWindowFilter(w.title, ui.filter)")).toBe(true);
  });
});

describe('the "?" sheet', () => {
  const SHEET = normCode("components/palette/ShortcutsSheet.tsx");

  it("takes focus when it opens", () => {
    expect(has(SHEET, "if (open) cardRef.current?.focus();")).toBe(true);
    expect(has(SHEET, '<div className="shortcuts-card" ref={cardRef} tabIndex={-1}')).toBe(true);
  });

  it("makes a rebindable row a keyboard button: Enter or Space starts recording", () => {
    expect(has(SHEET, 'role={meta ? "button" : undefined}')).toBe(true);
    expect(has(SHEET, "tabIndex={meta ? 0 : undefined}")).toBe(true);
    expect(has(SHEET, 'e.key !== "Enter" && e.key !== " "')).toBe(true);
    // A key pressed on the row's own + / ↺ is theirs, not the row's.
    expect(has(SHEET, "e.target !== e.currentTarget")).toBe(true);
    expect(has(normCode("components/palette/ShortcutsSheet.css"), ".shortcut-row.rebindable:focus-visible")).toBe(true);
  });

  it("Restore defaults keeps a copy and offers Undo; ↺ says what it restored", () => {
    expect(has(SHEET, "const saved = keyOverridesSnapshot(); resetAllOverrides();")).toBe(true);
    expect(has(SHEET, 'toast("Restored the default shortcuts", { action: { label: "Undo", onClick: () => restoreKeyOverrides(saved) }')).toBe(true);
    expect(has(SHEET, '"Restored the default for " + (meta.kind === "chord" ? CHORDS[meta.id].desc : desc)')).toBe(true);
    expect(SHEET).not.toContain("Shortcuts restored to defaults");
  });

  it("the Undo puts every custom key and chord back", () => {
    setKeyCombos("palette", [{ key: "j", mod: true }]);
    const chord = Object.keys(CHORDS)[0];
    setChordKey(chord, "9");
    const saved = keyOverridesSnapshot();
    resetAllOverrides();
    expect(getKeyOverride("palette")).toBeUndefined();
    expect(chordKeyFor(chord)).toBe(chord);
    restoreKeyOverrides(saved);
    expect(getKeyOverride("palette")).toEqual([{ key: "j", mod: true }]);
    expect(chordKeyFor(chord)).toBe("9");
    // A copy, not the live object: a later change doesn't rewrite the snapshot.
    setKeyCombos("palette", [{ key: "q", mod: true }]);
    expect(saved.keys.palette).toEqual([{ key: "j", mod: true }]);
  });

  it("the Talk row names Alt+Space, which a browser tab leaves to the page", () => {
    expect(KEYMAP.find((e) => e.id === "talk")!.help![1]).toBe("Ctrl+T / Alt+Space");
  });
});

describe("the GitHub bars match the Tickets bar", () => {
  for (const [file, short] of [
    ["components/sidebar/PrReviewBar.tsx", "Auto-address"],
    ["components/sidebar/GitIssueBar.tsx", "Auto-start"],
  ] as const) {
    it(file + ": a named switch and a red dot on a network problem", () => {
      const c = normCode(file);
      expect(has(c, `<span className="dc-sw-label">${short}</span>`)).toBe(true);
      expect(has(c, "aria-label={toggleLabel}")).toBe(true);
      expect(has(c, '(netIssue ? "dc-error" : active ? "on" : !on ? "off" : "idle")')).toBe(true);
      expect(has(c, '? "Connection problems — see Settings → System logs" : "No network connection"')).toBe(true);
    });
  }

  it("the shared hook reads offline and the pipeline's net_error", () => {
    const hook = normCode("components/sidebar/useGithubToggleBar.ts");
    expect(has(hook, "const netIssue = !online || !!ingestion?.net_error;")).toBe(true);
    expect(has(hook, "useSyncExternalStore(subscribeOnline, () => navigator.onLine)")).toBe(true);
  });

  it("Customize → Sidebar calls them what the bars and Intake call them", () => {
    const label = (k: string) => SIDEBAR_BARS.find((b) => b.key === k)?.label;
    expect(label("pr-review")).toBe("GitHub PRs");
    expect(label("issue-handling")).toBe("GitHub issues");
  });
});

describe("a team-run header works from the keyboard", () => {
  const RGH = "components/sidebar/RunGroupHeader.tsx";

  it("its caret and name are a real fold button", () => {
    const head = fn(RGH, "RunGroupHeader");
    expect(has(head, '<button type="button" className="dev-toggle" aria-expanded={!group.collapsed}>')).toBe(true);
    expect(has(head, "aria-expanded={!!menu}")).toBe(true);
    // The ⋯ shows while anything in the header has focus, so Tab can reach it.
    expect(has(normCode("components/sidebar/RunGroup.css"), ".run-group-head:focus-within .rg-more")).toBe(true);
  });

  it("its menu focuses an item on open and walks them with ↑ / ↓", () => {
    const menu = fn(RGH, "RunGroupMenu");
    expect(has(menu, "(back || menuItems(root)[0] || root).focus();")).toBe(true);
    expect(has(menu, 'if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;')).toBe(true);
    expect(has(menu, "onClose(true);")).toBe(true);
    expect(stepMenu(3, -1, 1)).toBe(0);
    expect(stepMenu(3, -1, -1)).toBe(2);
    expect(stepMenu(3, 2, 1)).toBe(0);
    expect(stepMenu(3, 0, -1)).toBe(2);
    expect(stepMenu(3, 1, 1)).toBe(2);
  });

  it("Cancel… focuses the safe answer, Keep going", () => {
    expect(has(fn(RGH, "RunGroupMenu"), '<button type="button" autoFocus onClick={() => setMode("")}> Keep going')).toBe(true);
  });
});

describe("the guest bar", () => {
  const BAR = normCode("components/sidebar/GuestBar.tsx");

  it("rests while the window is hidden and catches up when shown", () => {
    expect(has(BAR, "const stop = pollWhileVisible(() => void tick(), POLL_MS);")).toBe(true);
    expect(BAR).not.toContain("setInterval(");
  });

  it("shows a failed Kill as an error card", () => {
    expect(has(BAR, 'errorPop("Kill guest links failed", errMsg(e));')).toBe(true);
    expect(BAR).not.toContain("e instanceof Error ? e.message");
  });
});
