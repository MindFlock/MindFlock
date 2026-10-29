/** Round 4 — the main screen and its glue: calm decided before first paint,
 * one per-session menu, the pane's Task line and Resume, middle-truncated
 * names, surfaces in the URL hash, shortcut text from the live keymap, and a
 * quieter main screen. */

import { describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";

/** A Map-backed localStorage, installed BEFORE the store module loads. */
const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});

const { useUi } = await import("../state/store");
const { CALM_HINT_KEY, calmDecision, readCalmHint, undecidedCalm, writeCalmHint } = await import("../lib/calm");
const { splitName } = await import("../lib/midName");
const { sessionMenu, hasUncommitted } = await import("../lib/sessionMenu");
const { actionCtx } = await import("../lib/sessionActionTable");
const { hashFor, parseHash, installSurfaceHash } = await import("../lib/surfaceHash");
const { shortcutLabels } = await import("../lib/keymap");
const { browserReserved } = await import("../components/palette/ShortcutsSheet");
const { firstLine } = await import("../lib/format");
import type { Instance } from "../api/types";

describe("calm before first paint", () => {
  it("keeps the last server-made decision as a render hint", () => {
    storage.clear();
    expect(readCalmHint()).toBeNull();
    writeCalmHint(true);
    expect(storage.get(CALM_HINT_KEY)).toBe("1");
    expect(readCalmHint()).toBe(true);
    writeCalmHint(false);
    expect(readCalmHint()).toBe(false);
  });

  it("is undecided only while the server hasn't said enough", () => {
    // Setup record in flight, onboarded veteran: undecided (the hint answers).
    expect(calmDecision({ sessions: 1, status: undefined, onboarded: true })).toBeNull();
    // A fresh install decides at once; three sessions decide at once.
    expect(calmDecision({ sessions: 0, status: undefined, onboarded: false })).toBe(true);
    expect(calmDecision({ sessions: 3, status: undefined, onboarded: true })).toBe(false);
  });

  it("useCalm reads the hint only while undecided, and writes back the server's answer", () => {
    const hook = code("lib/useCalm.ts");
    expect(hook).toContain("if (decided !== null) writeCalmHint(decided);");
    expect(hook).toContain("if (decided !== null) return decided;");
    expect(hook).toContain("const hint = readCalmHint();");
    // No hint at all: calm until the server answers — never dense-then-calm
    // (r5-new finding 5: the full chrome flashed ~5 s on one load in three).
    expect(hook).toContain("return undecidedCalm(hint);");
    expect(undecidedCalm(null)).toBe(true);
    expect(undecidedCalm(false)).toBe(false);
    expect(undecidedCalm(true)).toBe(true);
  });
});

describe("middle truncation keeps the suffix that tells sessions apart", () => {
  it("splits long names with the numeric suffix in the tail", () => {
    const [head, tail] = splitName("add-readme-section-about-setup-2");
    expect(head + tail).toBe("add-readme-section-about-setup-2");
    expect(tail.endsWith("-2")).toBe(true);
    expect(tail.length).toBeGreaterThan(2);
    expect(splitName("short-2")).toEqual(["short-2", ""]);
    const [, t3] = splitName("fix the login flow (3)");
    expect(t3.endsWith("(3)")).toBe(true);
  });

  it("is drawn in the rail row and the pane title", () => {
    expect(code("components/sidebar/SidebarRow.tsx")).toContain("<MidName text={shown} />");
    expect(code("components/grid/Pane.tsx")).toContain("<MidName text={displayName} />");
  });
});

describe("one per-session menu", () => {
  const caps = { git: true, tailscale: true, ticketing: true, github: true };
  const menu = (o: Partial<Instance> = {}, opts = {}) => {
    const inst = { title: "s", status: "ready", branch: "me/b", folder: "/w/s", ...o } as Instance;
    return sessionMenu(inst, actionCtx("s", { inst, caps }), opts);
  };
  const keys = (m: ReturnType<typeof menu>) => m.groups.flatMap((g) => g.items.map((i) => i.key));

  it("groups Work · Ship · Session · Danger, rare items under More", () => {
    const m = menu();
    expect(m.groups.map((g) => g.id)).toEqual(["work", "ship", "session", "danger"]);
    expect(m.groups.find((g) => g.id === "danger")!.items.map((i) => i.key)).toEqual(["close", "delete"]);
    const more = m.more.map((i) => i.key);
    for (const rare of ["split", "duplicate", "ide", "hide", "terminal", "map", "copy-path"]) expect(more).toContain(rare);
    for (const rare of ["split", "duplicate", "ide", "hide"]) expect(keys(m)).not.toContain(rare);
  });

  it("offers Commit… only once there is something to commit", () => {
    expect(keys(menu())).not.toContain("commit");
    expect(keys(menu({ diff_stat: { uncommitted: { files: 1, additions: 0, deletions: 0 } } } as Partial<Instance>))).toContain(
      "commit"
    );
    expect(hasUncommitted({ diff_stat: null } as Partial<Instance>)).toBe(false);
  });

  it("the pane leaves out the tabs its header shows and adds history", () => {
    const m = menu({}, { visibleTabs: ["shell", "map"], onHistory: () => {} });
    const more = m.more.map((i) => i.key);
    expect(more).not.toContain("terminal");
    expect(more).not.toContain("map");
    expect(more).toContain("history");
  });

  it("a session whose folder is gone gets only Delete", () => {
    const m = menu({ workspace_missing: true });
    expect(keys(m)).toEqual(["delete"]);
    expect(m.more).toEqual([]);
  });

  it("both hosts draw the same model", () => {
    expect(code("components/sidebar/SidebarRow.tsx")).toContain("model={sessionMenu(inst, ctx, {");
    expect(code("components/grid/Pane.tsx")).toContain("sessionMenu(inst, menuCtx, {");
    // The row's opener is an SVG ⋮ at its right end (a › read as
    // expand/collapse, a ⋯ glyph beside the name looked out of place).
    // The two openers have distinct accessible names (the row's names its session).
    expect(code("components/sidebar/SidebarRow.tsx")).toMatch(/aria-label=\{"Session menu for " \+ shown\}[\s\S]{0,500}<KebabIcon \/>/);
  });
});

describe("the pane header", () => {
  const pane = code("components/grid/Pane.tsx");
  it("says what the session was asked to do", () => {
    expect(pane).toContain('<span className="prompt-pin-label">Task:</span>');
    // The transcript's newest prompt, else the task it was created with —
    // the server's Instance.Task, the same on every browser.
    expect(pane).toContain("const createTask = firstLine(inst.task);");
    expect(pane).toContain("const task = inst.last_prompt || createTask || null;");
    // The rail row reads the server's task headline first (TaskLine), then
    // the same prompt → create-task fallback as the pane.
    expect(code("components/sidebar/TaskLine.tsx")).toContain(
      "inst.task_headline || inst.last_prompt || firstLine(inst.task)",
    );
    expect(code("components/sidebar/SidebarRow.tsx")).toContain("taskLineOf(inst).text");
    expect(firstLine("\n  add a README section\nwith details")).toBe("add a README section");
    expect(firstLine(null)).toBe("");
  });

  it("has no Pause button — only ▶ Resume, on a paused session — and no Commit… with nothing to commit", () => {
    expect(pane).not.toContain("pane-pause");
    expect(pane).not.toContain("❚❚");
    expect(pane).toContain('const canResume = sessionAction("resume").enabled(menuCtx);');
    expect(pane).toMatch(/\{canResume && \(\s*<button\s+className="act pane-resume"/);
    // ...and none while a flight drives the session: its strip is the next step.
    expect(pane).toContain(
      '(inst.flight && isLive(inst.flight)) || (ns && ns.label === "Commit…" && !hasUncommitted(inst)) ? null : ns'
    );
  });
});

describe("surfaces in the URL hash", () => {
  it("names the five top-bar surfaces, and nothing else", () => {
    expect(hashFor("settings", "accounts")).toBe("#settings/accounts");
    expect(hashFor("intake", "tickets")).toBe("#intake/tickets");
    expect(hashFor("new-session", null)).toBe("#new");
    expect(hashFor("verify", null)).toBe("#verify");
    expect(hashFor("schedule", "new")).toBe("#schedule");
    expect(hashFor("schedule", null)).toBe("#schedule");
    // Schedule was Templates: the old name and an old link both land on it.
    expect(hashFor("templates", "new")).toBe("#schedule");
    expect(parseHash("#templates")).toEqual({ name: "schedule", target: null });
    expect(parseHash("#schedule")).toEqual({ name: "schedule", target: null });
    expect(hashFor("commit", "x")).toBe("");
    expect(hashFor("palette", null)).toBe("");
    expect(parseHash("#settings/accounts")).toEqual({ name: "settings", target: "accounts" });
    expect(parseHash("#new")).toEqual({ name: "new-session", target: null });
    expect(parseHash("#nope")).toBeNull();
    expect(parseHash("")).toBeNull();
    // Flights: its tab and the tab's own deep link after a slash.
    expect(hashFor("flights", null)).toBe("#flights");
    expect(hashFor("flights", "board")).toBe("#flights");
    expect(hashFor("flights", "plans")).toBe("#flights/plans");
    expect(hashFor("flights", "checklists/feature/x")).toBe("#flights/checklists/feature%2Fx");
    expect(hashFor("flights", "schedule/new")).toBe("#flights/schedule");
    expect(parseHash("#flights/plans")).toEqual({ name: "flights", target: "plans" });
    expect(parseHash("#flights/checklists/feature%2Fx")).toEqual({ name: "flights", target: "checklists/feature/x" });
  });

  it("Back closes a surface; closing it steps back over its entry; a link opens one", async () => {
    const listeners: Array<() => void> = [];
    const loc = { pathname: "/", search: "", hash: "#intake/prs" };
    const calls: string[] = [];
    const win = {
      location: loc,
      history: {
        pushState: (_d: unknown, _u: string, url: string) => {
          calls.push("push " + url);
          loc.hash = url.slice(url.indexOf("#")) || "";
        },
        replaceState: (_d: unknown, _u: string, url: string) => {
          calls.push("replace " + url);
          loc.hash = url.includes("#") ? url.slice(url.indexOf("#")) : "";
        },
        back: () => {
          calls.push("back");
          loc.hash = "";
          listeners.forEach((f) => f());
        },
      },
      addEventListener: (_t: string, f: () => void) => void listeners.push(f),
      removeEventListener: () => {},
    } as unknown as Window;
    useUi.getState().closeDialog();
    const off = installSurfaceHash(win);
    // The old link opened Flights on Intake's tab (Sources → GitHub PRs),
    // rewritten in place to where it landed — no new history entry.
    expect(useUi.getState().openDialog).toBe("flights");
    expect(useUi.getState().dialogTarget).toBe("sources/prs");
    expect(calls).toEqual(["replace /#flights/sources/prs"]);
    // Closing it in the app strips the hash.
    useUi.getState().closeDialog();
    expect(calls).toEqual(["replace /#flights/sources/prs", "replace /"]);
    // Opening Settings pushes one entry; switching to Verify (Flights →
    // Checklists) replaces it.
    useUi.getState().openDialogFor("settings", "accounts");
    useUi.getState().openDialogFor("verify");
    expect(calls.slice(2)).toEqual(["push /#settings/accounts", "replace /#flights/checklists"]);
    // Browser Back: the hash goes, the surface closes.
    loc.hash = "";
    listeners.forEach((f) => f());
    expect(useUi.getState().openDialog).toBeNull();
    // Open again, then close in the app: it steps back over its own entry.
    useUi.getState().openDialogFor("new-session");
    useUi.getState().closeDialog();
    await Promise.resolve();
    expect(calls.slice(-2)).toEqual(["push /#new", "back"]);
    expect(useUi.getState().openDialog).toBeNull();
    // Close and open the next surface in one tick (Schedule → back to New):
    // a switch — the entry is replaced, never stepped back over. New's strip
    // still asks for "templates"; the store opens Flights → Schedule.
    useUi.getState().openDialogFor("templates", "new");
    expect(useUi.getState().openDialog).toBe("flights");
    useUi.getState().closeDialog();
    useUi.getState().openDialogFor("new-session");
    await Promise.resolve();
    expect(calls.slice(-2)).toEqual(["push /#flights/schedule", "replace /#new"]);
    expect(useUi.getState().openDialog).toBe("new-session");
    off();
  });

  it("is installed by App", () => {
    expect(code("App.tsx")).toContain("useEffect(() => installSurfaceHash(), []);");
  });
});

describe("shortcut text matches the real keymap", () => {
  it("New says Alt+N in a browser tab (Ctrl+N belongs to the browser)", () => {
    const inTab = shortcutLabels("new", (l) => !browserReserved(l));
    expect(inTab).toEqual(["Alt+N"]);
    expect(shortcutLabels("new")).toEqual(["Ctrl+N", "Alt+N"]);
    expect(shortcutLabels("intake")).toEqual(["Alt+I"]);
    expect(shortcutLabels("verify")).toEqual(["Alt+V"]);
    expect(shortcutLabels("palette")).toEqual(["Ctrl+P"]);
    const top = code("components/TopBar.tsx");
    expect(top).toContain('title={"New session" + keys("new")}');
    expect(top).not.toMatch(/\(Ctrl\+N\)/);
  });

  it("Esc closes the shortcuts sheet; changing keys is a mode you ask for", () => {
    const sheet = code("components/palette/ShortcutsSheet.tsx");
    expect(sheet).toMatch(/if \(e\.key !== "Escape"\) return;[\s\S]{0,120}closeDialog\(\);/);
    expect(sheet).toContain('{editing ? "Done" : "Change keys"}');
    expect(sheet).toContain("const meta = editing ? rowMeta : null;");
  });
});

describe("a quieter main screen", () => {
  it("drops the row's duplicate status dot (the chip says it)", () => {
    const row = code("components/sidebar/SidebarRow.tsx");
    expect(row).toContain('(disconnected || nested || nest.stem || lchip || pchip ? "" : " dot-quiet")');
  });

  it("the Setup chip goes neutral once the required steps are done", () => {
    expect(code("components/sidebar/Sidebar.tsx")).toContain('"setup-chip-slot" + (setupReqDone ? " req-done" : "")');
  });

  it("Verify is described one way", () => {
    // The top bar's entry is Flights now; Verify (its Checklists tab) keeps
    // the one blurb in the palette and the shortcut sheet.
    expect(code("components/palette/CommandPalette.tsx")).toContain("keywords: VERIFY_BLURB");
    expect(code("lib/keymap.ts")).toContain('"Flights → Checklists — " + VERIFY_BLURB');
    // The welcome card no longer describes Verify; the Verify tour does.
  });

  it('"Agent" is the coding agent only: the assistant\'s file is its Instructions', () => {
    expect(code("components/sidebar/SidebarBars.tsx")).toMatch(/>\s*Instructions\s*<\/button>/);
    expect(code("components/grid/SpecialPane.tsx")).toMatch(/>\s*Instructions\s*<\/button>/);
  });

  it("the welcome tour is marked seen when it closes, not when it opens", () => {
    const tour = code("components/onboarding/WelcomeTour.tsx");
    const due = tour.slice(tour.indexOf("if (!due) return;"), tour.indexOf("}, [due]);"));
    expect(due).not.toContain("markWelcomeTourSeen");
    expect(tour).toMatch(/else if \(wasOpen\)[\s\S]{0,120}markWelcomeTourSeen\(\)/);
  });
});
