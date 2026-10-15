/** Settings: basic vs advanced (components/settings/settingsMode.tsx).
 *
 * Pinned here:
 *  - basic mode (the default) lists only the six screens a new user needs;
 *    every other screen waits behind "Show advanced settings";
 *  - advanced mode lists every screen, under its group headings;
 *  - a deep link to an advanced screen still opens it in basic mode, listed
 *    under an "Advanced" heading, without flipping the saved mode;
 *  - the mode is a synced pref (`settings_advanced`, cached in
 *    localStorage `mf_settings_advanced`), never a per-browser flag;
 *  - the device / phone / Tailscale-flavoured controls on basic screens sit
 *    in a collapsed Advanced fold. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { src } from "./sourceText";

const store: Record<string, string> = {};
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (k in store ? store[k] : null),
  setItem: (k: string, v: string) => {
    store[k] = String(v);
  },
  removeItem: (k: string) => {
    delete store[k];
  },
});

const { SettingsDialog } = await import("../components/settings/SettingsDialog");
const { ADVANCED_LS, navHeading, navScreens, readAdvancedMode, writeAdvancedMode } = await import(
  "../components/settings/settingsMode"
);
const { pendingFields, prefFieldFor, PREF_DEFAULTS } = await import("../lib/prefs");
const { useUi } = await import("../state/store");
const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");

const BASIC = ["general", "connections", "notifications", "coding", "workspace", "appearance"];
const ADVANCED = [
  "providers",
  "accounts",
  "localmodel",
  "orchestration",
  "ide",
  "security",
  "devices",
  "mobile",
  "peer",
  "extensions",
  "doctor",
  "logs",
  "advanced",
];

/** Render the open dialog (optionally deep-linked) and read its nav. */
function renderNav(target: string | null = null) {
  useUi.setState({ openDialog: "settings", dialogTarget: target } as never);
  // A server render reads the store's INITIAL state (useSyncExternalStore's
  // server snapshot): copy the live state into it (as flockRail.test does).
  Object.assign(useUi.getInitialState(), useUi.getState());
  const client = new QueryClient();
  const html = renderToStaticMarkup(
    createElement(QueryClientProvider, { client }, createElement(SettingsDialog, {}))
  );
  const nav = html.split('id="settings-nav"', 2)[1].split("</nav>", 1)[0];
  const items = [...nav.matchAll(/class="set-nav-item[^"]*" data-screen="([^"]+)"/g)].map((m) => m[1]);
  const headings = [...nav.matchAll(/class="set-nav-group"[^>]*>([^<]+)</g)].map((m) => m[1]);
  const active = nav.match(/class="set-nav-item active" data-screen="([^"]+)"/)?.[1];
  return { html, nav, items, headings, active };
}

beforeEach(() => {
  for (const k of Object.keys(store)) delete store[k];
});
afterEach(() => {
  useUi.setState({ openDialog: null, dialogTarget: null } as never);
  Object.assign(useUi.getInitialState(), useUi.getState());
});

describe("basic mode (the default)", () => {
  it("lists only the basic screens, flat, with the toggle at the foot", () => {
    const { items, headings, nav, active } = renderNav();
    expect(items).toEqual(BASIC);
    expect(headings).toEqual([]);
    // Round 5: Settings opens on Connections, the status page (r5-settings #4).
    expect(active).toBe("connections");
    expect(nav).toContain('data-mode="basic"');
    expect(nav).toMatch(/id="settings-advanced-toggle"[^>]*aria-pressed="false"[^>]*>Show advanced settings</);
    expect(nav).toContain(ADVANCED.length + " more screens");
  });

  it("never lists a device, phone, people or troubleshooting screen", () => {
    const { items } = renderNav();
    for (const k of ADVANCED) expect(items, k).not.toContain(k);
  });
});

describe("advanced mode", () => {
  it("the persisted choice reveals every screen under its group headings", () => {
    store[ADVANCED_LS] = "true";
    const { items, headings, nav } = renderNav();
    expect(items).toEqual(expect.arrayContaining([...BASIC, ...ADVANCED]));
    expect(items).toHaveLength(BASIC.length + ADVANCED.length);
    expect(headings).toEqual(["Agents", "Code", "This device", "Devices and people", "Extensions", "Troubleshooting"]);
    expect(nav).toContain('data-mode="advanced"');
    expect(nav).toMatch(/aria-pressed="true"[^>]*>Hide advanced settings</);
    expect(nav).not.toContain("more screens");
  });

  it("the toggle's write is the synced pref, not a per-browser flag", () => {
    expect(ADVANCED_LS).toBe("mf_settings_advanced");
    expect(prefFieldFor(ADVANCED_LS)).toBe("settings_advanced");
    expect(PREF_DEFAULTS.settings_advanced).toBeNull();
    expect(readAdvancedMode()).toBe(false);
    writeAdvancedMode(true);
    expect(store[ADVANCED_LS]).toBe("true");
    expect(readAdvancedMode()).toBe(true);
    // Queued for POST /api/prefs (so settings sync carries it to every device).
    expect(pendingFields().has("settings_advanced")).toBe(true);
    writeAdvancedMode(false);
    expect(readAdvancedMode()).toBe(false);
    expect(renderNav().items).toEqual(BASIC);
  });

  it("the dialog wires the toggle to that write", () => {
    const dialog = src("components/settings/SettingsDialog.tsx");
    expect(dialog).toContain("<AdvancedToggle advanced={advanced}");
    expect(dialog).toMatch(/const changeMode = \(on: boolean\) => \{\s*setAdvanced\(on\);\s*writeAdvancedMode\(on\);/);
  });
});

describe("deep links to an advanced screen", () => {
  for (const [target, key] of [
    ["devices", "devices"],
    ["mobile", "mobile"],
    ["doctor", "doctor"],
    ["providers", "providers"],
    // A legacy / feature-named key resolves first (screenKeys.ts).
    ["tailnet-trust", "devices"],
    ["your-phone", "mobile"],
  ] as const) {
    it(`${target} opens ${key} in basic mode, last, under an "Advanced" heading`, () => {
      const { items, headings, active, html } = renderNav(target);
      expect(active).toBe(key);
      // The basic six in order, then the linked screen LAST — so its
      // "Advanced" heading never sits over Workspace or Appearance.
      expect(items).toEqual([...[...BASIC].sort((a, b) => ORDER.indexOf(a) - ORDER.indexOf(b)), key]);
      expect(headings).toEqual(["Advanced"]);
      // The screen itself is the one rendered.
      expect(html).toMatch(new RegExp('class="set-screen active" data-screen="' + key + '"'));
      // …and the saved mode did not flip.
      expect(store[ADVANCED_LS]).toBeUndefined();
    });
  }

  it("a basic deep link stays basic", () => {
    const { active, headings } = renderNav("workspace");
    expect(active).toBe("workspace");
    expect(headings).toEqual([]);
  });
});

/** The full nav order (SCREENS in SettingsDialog.tsx, less dev-only Site traffic). */
const ORDER = [
  "general",
  "connections",
  "notifications",
  "coding",
  "providers",
  "accounts",
  "localmodel",
  "orchestration",
  "workspace",
  "ide",
  "security",
  "appearance",
  "devices",
  "mobile",
  "peer",
  "extensions",
  "doctor",
  "logs",
  "advanced",
];

describe("navScreens / navHeading", () => {
  const S = [
    { key: "a", group: undefined, advanced: false },
    { key: "b", group: "G", advanced: false },
    { key: "c", group: "G", advanced: true },
    { key: "d", group: "H", advanced: true },
  ];
  it("filters by mode and keeps the active screen", () => {
    expect(navScreens(S, false, "a").map((s) => s.key)).toEqual(["a", "b"]);
    expect(navScreens(S, false, "d").map((s) => s.key)).toEqual(["a", "b", "d"]);
    expect(navScreens(S, true, "a").map((s) => s.key)).toEqual(["a", "b", "c", "d"]);
  });
  it("headings: groups in advanced mode, only 'Advanced' over a peek in basic", () => {
    const all = navScreens(S, true, "");
    expect(all.map((_, i) => navHeading(all, i, true))).toEqual(["", "G", "", "H"]);
    const peek = navScreens(S, false, "d");
    expect(peek.map((_, i) => navHeading(peek, i, false))).toEqual(["", "", "Advanced"]);
  });
});

describe("advanced controls on basic screens are folded", () => {
  const folds: Array<[string, string, RegExp]> = [
    // Phone push (ntfy) — the phone, during basic use.
    // Optional, not expert: these two stay on the basic screen, folded.
    ["screens/Notifications.tsx", "notif-advanced", /<AdvancedFold id="notif-advanced" summary="Phone alerts \(ntfy\)" basic>\s*<Ntfy /],
    ["screens/Notifications.tsx", "notif-more-events", /<AdvancedFold\s+id="notif-more-events"\s+basic/],
    ["screens/CodingCli.tsx", "coding-advanced", /<AdvancedFold id="coding-advanced">\s*<WindowRefresh \/>/],
    ["screens/General.tsx", "general-advanced", /<ScrollSpeedRow \/>[\s\S]*<AdvancedFold id="general-advanced" summary="Budgets, resume and sleep" defaultOpen=\{false\}>[\s\S]*window_budget_usd/],
    ["screens/Workspace.tsx", "workspace-advanced", /<AdvancedFold id="workspace-advanced">[\s\S]*live_branch[\s\S]*fasttrack_depth[\s\S]*precommit_retry_hooks/],
    // Folded by default even in advanced mode (r5 settings #8).
    ["screens/Connections.tsx", "connections-advanced", /<AdvancedFold id="connections-advanced" summary="More connections" defaultOpen=\{false\}>\s*\{more\.map\(renderGroup\)\}/],
    // "Other agents on this computer" is a plain fold on purpose: visible
    // (closed) in basic mode too — agentJourney.test.ts pins it.
  ];
  for (const [file, id, shape] of folds) {
    it(file + " folds its advanced part", () => {
      const text = src("components/settings/" + file);
      expect(text).toContain('import { AdvancedFold');
      expect(text, id).toMatch(shape);
    });
  }

  it("Connections keeps devices, orchestration and the rest out of the basic list", async () => {
    const { BASIC_KINDS, isBasicKind } = await import("../components/settings/screens/Connections");
    expect([...BASIC_KINDS].sort()).toEqual(["agent", "github", "tracker"]);
    for (const k of ["devices", "mcp", "database", "notifications", "extension", "system", undefined])
      expect(isBasicKind(k), String(k)).toBe(false);
  });

  it("the fold is absent in basic mode (unless `basic`) and open in advanced mode", () => {
    const mode = src("components/settings/settingsMode.tsx");
    // Open in advanced mode unless the fold says `defaultOpen={false}`.
    expect(mode).toContain("const initial = defaultOpen ?? advanced;");
    expect(mode).toContain("const [open, setOpen] = useState(initial);");
    expect(mode).toContain("useEffect(() => setOpen(initial), [initial]);");
    // Round 4: one advanced mechanism — basic mode renders no fold at all.
    expect(mode).toContain("if (!advanced && !basic) return null;");
  });

  it("only Notifications' optional folds opt back into basic mode", () => {
    for (const f of ["General", "Workspace", "CodingCli", "Connections"]) {
      const text = src("components/settings/screens/" + f + ".tsx");
      expect(text, f).not.toMatch(/<AdvancedFold[^>]*\sbasic[\s>]/);
    }
  });

  it("the basic screens don't tell a newcomer about other devices", () => {
    for (const f of ["General", "Workspace", "Appearance", "CodingCli"]) {
      const text = src("components/settings/screens/" + f + ".tsx");
      expect(text, f).not.toMatch(/Tailscale|your devices|Your phone/i);
    }
  });
});
