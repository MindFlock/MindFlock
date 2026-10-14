/** Light / dark as one value: the top bar's moon and Settings → Appearance →
 * Mode both call lib/theme.ts `setTheme`, which the store mirrors; a first
 * launch follows the OS until somebody chooses. */

/// <reference types="vite/client" />
import { describe, expect, it, vi } from "vitest";
import INDEX_HTML from "../../index.html?raw";
import TOPBAR from "../components/TopBar.tsx?raw";
import APPEARANCE from "../components/settings/screens/Appearance.tsx?raw";
import PREFS_SYNC from "../lib/prefsSync.ts?raw";

const notePrefWrite = vi.fn();
vi.mock("../lib/prefs", async (orig) => ({
  ...((await orig()) as object),
  notePrefWrite: (k: string) => notePrefWrite(k),
}));

const { setTheme } = await import("../lib/theme");
const { useUi } = await import("../state/store");
const { THEME_CHANGED } = await import("../lib/prefs");

/** Runs index.html's pre-paint script against a fake browser. */
function prePaint(saved: string | null, osLight: boolean): boolean {
  const script = INDEX_HTML.match(/<script>(try\{var _t=[\s\S]*?)<\/script>/)?.[1];
  expect(script, "pre-paint script").toBeTruthy();
  const classes = new Set<string>();
  const document = {
    documentElement: { classList: { add: (c: string) => classes.add(c) }, setAttribute() {} },
  };
  const localStorage = { getItem: (k: string) => (k === "cs_theme" ? saved : null) };
  const window = { matchMedia: (q: string) => ({ matches: osLight && q.includes("light") }) };
  new Function("document", "localStorage", "window", script!)(document, localStorage, window);
  return classes.has("light");
}

describe("first paint", () => {
  it("a saved choice wins over the OS", () => {
    expect(prePaint("light", false)).toBe(true);
    expect(prePaint("dark", true)).toBe(false);
  });

  it("never chosen follows the OS colour scheme", () => {
    expect(prePaint(null, true)).toBe(true);
    expect(prePaint(null, false)).toBe(false);
  });
});

describe("setTheme", () => {
  it("paints, caches, syncs and tells the store and the top bar", () => {
    const toggled: Array<[string, boolean]> = [];
    const events: string[] = [];
    const stored: Record<string, string> = {};
    vi.stubGlobal("document", {
      documentElement: {
        classList: { toggle: (c: string, on: boolean) => toggled.push([c, on]), contains: () => false },
      },
      dispatchEvent: (e: Event) => events.push(e.type),
    });
    vi.stubGlobal("localStorage", { setItem: (k: string, v: string) => (stored[k] = v) });
    notePrefWrite.mockReset();

    setTheme(true);
    expect(toggled).toEqual([["light", true]]);
    expect(stored.cs_theme).toBe("light");
    expect(notePrefWrite).toHaveBeenCalledWith("cs_theme");
    expect(useUi.getState().themeLight).toBe(true);
    expect(events).toEqual([THEME_CHANGED]);

    setTheme(false);
    expect(stored.cs_theme).toBe("dark");
    expect(useUi.getState().themeLight).toBe(false);
    vi.unstubAllGlobals();
  });
});

describe("both controls drive the one value", () => {
  it("the moon reads the store and calls setTheme", () => {
    expect(TOPBAR).toContain("const light = useUi((s) => s.themeLight);");
    expect(TOPBAR).toContain("const toggleTheme = () => setTheme(!light);");
    expect(TOPBAR).not.toContain('localStorage.setItem("cs_theme"');
  });

  it("Settings → Appearance has a Dark / Light Mode control", () => {
    expect(APPEARANCE).toContain('<h3 className="set-section-title">Mode</h3>');
    expect(APPEARANCE).toContain('id="mode-swatches"');
    expect(APPEARANCE).toContain("onClick={() => setTheme(isLight)}");
    expect(APPEARANCE).not.toContain("Light or dark is the moon button in the top bar.");
  });

  it("a theme adopted from another device updates the store too", () => {
    expect(PREFS_SYNC).toContain('st.themeLight = v === "light";');
  });
});
