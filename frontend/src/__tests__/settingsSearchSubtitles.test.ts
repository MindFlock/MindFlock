/** Settings search, round 5 follow-up: every result says what is behind it.
 * "phone" offered "Notifications → Phone alerts" and "Your phone" with
 * nothing telling them apart, "tour" a bare "General", "dark" a bare
 * "Appearance". Each entry now carries a one-line `sub`, drawn small under
 * the title in the Settings box and in the palette, and the ranking puts a
 * title match above a subtitle match. */
import { describe, expect, it } from "vitest";
import {
  SETTINGS_SEARCH,
  searchSettings,
  settingsActions,
  settingsMatchScore,
  type SettingsSearchEntry,
} from "../components/palette/settingsSearch";
import searchBoxSrc from "../components/settings/SettingsSearch.tsx?raw";
import searchBoxCss from "../components/settings/SettingsSearch.css?raw";
import paletteSrc from "../components/palette/CommandPalette.tsx?raw";
import paletteCss from "../components/palette/CommandPalette.css?raw";
import generalSrc from "../components/settings/screens/General.tsx?raw";
import appearanceSrc from "../components/settings/screens/Appearance.tsx?raw";

const hits = (q: string) => searchSettings(q).map((e) => [e.label, e.sub]);

describe("settings search subtitles", () => {
  it("every index entry has a non-empty one-line subtitle", () => {
    for (const e of SETTINGS_SEARCH) {
      expect(e.sub.trim(), e.label).not.toBe("");
      expect(e.sub, e.label).not.toMatch(/\n/);
      expect(e.sub.length, e.label).toBeLessThanOrEqual(80);
      // The subtitle adds something; it doesn't repeat the title.
      expect(e.sub.toLowerCase(), e.label).not.toBe(e.label.toLowerCase());
    }
  });

  it("labels stay unique (React keys) and every subtitle is its own", () => {
    expect(new Set(SETTINGS_SEARCH.map((e) => e.label)).size).toBe(SETTINGS_SEARCH.length);
    expect(new Set(SETTINGS_SEARCH.map((e) => e.sub)).size).toBe(SETTINGS_SEARCH.length);
  });

  it("'phone' tells push alerts apart from using MindFlock on the phone", () => {
    const r = hits("phone");
    expect(r[0]).toEqual(["Notifications → Phone alerts", "Push notifications to your phone (ntfy)"]);
    expect(r[1]).toEqual(["Your phone", "Use MindFlock itself from your phone, over Tailscale"]);
  });

  it("'tour' and 'dark' land on the control, not a bare screen name", () => {
    expect(hits("tour")[0]).toEqual(["General → Tours", "Replay the welcome tour and the menu tours"]);
    expect(hits("dark")[0]).toEqual(["Appearance → Mode", "Light or dark (the moon in the top bar switches too)"]);
    expect(hits("parallel").map((h) => h[0])).toEqual(["Agents → Sessions running at once", "Agent orchestration"]);
  });

  it("the new control entries scroll to anchors their screens really have", () => {
    const tours = SETTINGS_SEARCH.find((e) => e.label === "General → Tours")!;
    const mode = SETTINGS_SEARCH.find((e) => e.label === "Appearance → Mode")!;
    expect(tours.anchor).toBe("#general-tours");
    expect(generalSrc).toContain('id="general-tours"');
    expect(mode.anchor).toBe("#mode-swatches");
    expect(appearanceSrc).toContain('id="mode-swatches"');
  });

  it("a title match ranks above a subtitle match", () => {
    const byTitle: SettingsSearchEntry = { key: "general", label: "Alpha widgets", sub: "Nothing here", terms: [] };
    const bySub: SettingsSearchEntry = { key: "general", label: "Beta", sub: "Tune the widgets", terms: [] };
    expect(settingsMatchScore(byTitle, "widgets")).toBeGreaterThanOrEqual(0);
    expect(settingsMatchScore(bySub, "widgets")).toBeGreaterThan(settingsMatchScore(byTitle, "widgets"));
    // In the real index: "tour" is in General's subtitle and in the Tours title.
    const labels = searchSettings("tour").map((e) => e.label);
    expect(labels.indexOf("General → Tours")).toBeLessThan(labels.indexOf("General"));
    // The subtitle is searched only for whole-word starts of 3+ letters.
    expect(settingsMatchScore(bySub, "wi")).toBe(-1);
    expect(settingsMatchScore(bySub, "idgets")).toBe(-1);
  });

  it("palette rows carry the subtitle", () => {
    const row = settingsActions("phone")[0];
    expect(row.label).toBe("Settings → Notifications → Phone alerts");
    expect(row.sub).toBe("Push notifications to your phone (ntfy)");
  });

  it("both result lists draw it small under the title", () => {
    expect(searchBoxSrc).toContain('<span className="set-search-sub">{h.sub}</span>');
    expect(searchBoxSrc).toContain('import "./SettingsSearch.css";');
    expect(searchBoxCss).toContain(".set-search-sub");
    expect(paletteSrc).toContain('<span className="palette-sub">{a.sub}</span>');
    expect(paletteCss).toContain(".palette-item .palette-sub");
    expect(paletteCss).toContain(".palette-item.selected .palette-sub");
  });
});
