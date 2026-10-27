/** Settings reopens on the screen you last had open (for this page's life):
 * people come back to the setting they were on. A deep link still wins and a
 * screen that no longer exists falls back to General. */

import { beforeEach, describe, expect, it } from "vitest";

import { lastSettingsScreen, openingScreen, rememberSettingsScreen } from "../lib/settingsLastScreen";
import { DEFAULT_SETTINGS_SCREEN } from "../lib/openSettings";

const KEYS = ["general", "appearance", "voice", "notifications"];

beforeEach(() => rememberSettingsScreen(""));

describe("the screen Settings opens on", () => {
  it("is General until a screen was chosen", () => {
    expect(lastSettingsScreen()).toBeNull();
    expect(openingScreen(null, KEYS)).toBe(DEFAULT_SETTINGS_SCREEN);
  });

  it("is the one you last had open", () => {
    rememberSettingsScreen("voice");
    expect(lastSettingsScreen()).toBe("voice");
    expect(openingScreen(null, KEYS)).toBe("voice");
    expect(openingScreen("", KEYS)).toBe("voice");
  });

  it("a deep link still wins", () => {
    rememberSettingsScreen("voice");
    expect(openingScreen("notifications", KEYS)).toBe("notifications");
  });

  it("a screen that no longer exists (another mode, a retired key) falls back to General", () => {
    rememberSettingsScreen("doctor");
    expect(openingScreen(null, KEYS)).toBe(DEFAULT_SETTINGS_SCREEN);
  });
});


describe("the gear button opens Settings with no deep link", () => {
  it("openSettings() passes no target, so the last screen is used", async () => {
    const calls: unknown[] = [];
    const { useUi } = await import("../state/store");
    const prev = useUi.getState().openDialogFor;
    useUi.setState({ openDialogFor: ((name: string, target?: string | null) => calls.push([name, target ?? null])) as never });
    try {
      const { openSettings } = await import("../lib/openSettings");
      openSettings();
      openSettings("voice");
      expect(calls).toEqual([["settings", null], ["settings", "voice"]]);
    } finally {
      useUi.setState({ openDialogFor: prev });
    }
  });
});
