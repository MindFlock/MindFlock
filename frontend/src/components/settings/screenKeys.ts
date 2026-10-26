/** Settings screen keys: the stable ids deep links, the palette and the
 * server's Connections cards use. Labels changed when "Devices", "Mobile"
 * and "People" became Your devices / Your phone / People, and
 * three Security controls moved to Your devices; the keys did not, so every
 * old link still lands (same pattern as IntakeDialog's LEGACY_SCREEN_TABS,
 * and a pure module so tests need no React tree). */

/** Old or feature-named keys → the screen that holds them now. */
export const LEGACY_SCREEN_KEYS: Record<string, string> = {
  // The new names, so a link written after the rename works too.
  "your-devices": "devices",
  "your-phone": "mobile",
  phone: "mobile",
  // Voice moved off Your phone onto its own screen.
  "mobile-voice": "voice",
  speech: "voice",
  microphone: "voice",
  people: "peer",
  "peer-links": "peer",
  peers: "peer",
  // Controls that moved off Security onto Your devices.
  "remote-control": "devices",
  "tailnet-trust": "devices",
  "trusted-accounts": "devices",
  "pair-by-token": "devices",
  "settings-sync": "devices",
  "make-reachable": "devices",
  "shared-link": "devices",
  fleet: "devices",
};

/** The screen a key means today ("" when it is none we know). */
export function resolveScreenKey(name: string | null | undefined, known: readonly string[]): string {
  if (!name) return "";
  const key = LEGACY_SCREEN_KEYS[name] || name;
  return known.includes(key) ? key : "";
}
