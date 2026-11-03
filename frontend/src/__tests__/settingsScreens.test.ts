/** Settings' "Devices and people" screens: two ways to trust another machine,
 * each control on exactly one screen.
 *
 * Pinned here:
 *  - the legacy / feature-named screen keys (settings/screenKeys.ts) all
 *    resolve to a live screen, and the live keys themselves never changed
 *    ("devices", "mobile", "peer", "security"), so palette entries, docs
 *    links and deep links keep working after the renames;
 *  - the once-only rule: the access-token display, the Tailscale-mode switch
 *    and the trusted-accounts list are each rendered by ONE of the four
 *    screens (Your devices / Your phone / People / Security), found by
 *    grepping each screen's source plus the sibling screen modules it
 *    imports (Your devices renders its Advanced fold and the shared link
 *    from their own files);
 *  - the nav labels and the palette labels say Your devices / Your phone /
 *    People;
 *  - the ticket settings Intake owns (consolidation round): the ticket-session
 *    limit, worktree vs clone, Open IDE when a ticket starts and Ticket
 *    sessions in MindFlock are each drawn on ONE screen — Intake › Auto-start
 *    — and the Settings screens they left keep a "Set in Intake ›" row under
 *    the old name (which settings search still finds). */

import { describe, expect, it } from "vitest";
import { LEGACY_SCREEN_KEYS, resolveScreenKey } from "../components/settings/screenKeys";
import { searchSettings } from "../components/palette/settingsSearch";

/** Sources via Vite's glob rather than fs (this project installs no node
 * types): every settings screen module, keyed by its file name. */
const SCREEN_SRC: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob<string>("../components/settings/screens/*.tsx", { query: "?raw", import: "default", eager: true })
  ).map(([path, text]) => [path.replace(/^.*\//, ""), text])
);
const NAV = Object.values(
  import.meta.glob<string>("../components/settings/SettingsDialog.tsx", { query: "?raw", import: "default", eager: true })
)[0];
const PALETTE = Object.values(
  import.meta.glob<string>("../components/palette/CommandPalette.tsx", { query: "?raw", import: "default", eager: true })
)[0];

const FOUR = { devices: "Devices.tsx", mobile: "Mobile.tsx", peer: "PeerLinks.tsx", security: "Security.tsx" };

/** The screen's source plus every sibling screens/ module it imports,
 * transitively (what the screen renders, wherever the JSX lives). */
function closure(file: string): string {
  const seen = new Set<string>();
  const out: string[] = [];
  const walk = (f: string) => {
    if (seen.has(f) || !(f in SCREEN_SRC)) return;
    seen.add(f);
    const src = SCREEN_SRC[f];
    out.push(src);
    for (const m of src.matchAll(/from "\.\/([A-Za-z0-9_]+)"/g)) walk(m[1] + ".tsx");
  };
  walk(file);
  return out.join("\n");
}

const SRC = Object.fromEntries(Object.entries(FOUR).map(([k, f]) => [k, closure(f)])) as Record<
  keyof typeof FOUR,
  string
>;
const LIVE = [...NAV.matchAll(/\{ key: "([a-z-]+)", label: "([^"]+)"/g)].map((m) => ({ key: m[1], label: m[2] }));
const LIVE_KEYS = LIVE.map((s) => s.key);

describe("legacy screen keys route to the renamed screens", () => {
  it("finds the sources at all", () => {
    expect(Object.keys(SCREEN_SRC)).toContain("Devices.tsx");
    expect(NAV).toContain("SCREENS");
    expect(PALETTE).toContain("openSettings");
    expect(SRC.devices).toContain("DevicesAdvanced");
  });

  it("the four keys are unchanged and live", () => {
    for (const k of Object.keys(FOUR)) expect(LIVE_KEYS).toContain(k);
  });

  it("every alias resolves to a live screen", () => {
    for (const [alias, key] of Object.entries(LEGACY_SCREEN_KEYS)) {
      expect(LIVE_KEYS, alias + " → " + key).toContain(key);
      expect(resolveScreenKey(alias, LIVE_KEYS)).toBe(key);
    }
  });

  it("the new names and the moved controls land where they live now", () => {
    expect(resolveScreenKey("your-devices", LIVE_KEYS)).toBe("devices");
    expect(resolveScreenKey("your-phone", LIVE_KEYS)).toBe("mobile");
    expect(resolveScreenKey("people", LIVE_KEYS)).toBe("peer");
    expect(resolveScreenKey("peer-links", LIVE_KEYS)).toBe("peer");
    // Moved off Security onto Your devices → Advanced.
    expect(resolveScreenKey("remote-control", LIVE_KEYS)).toBe("devices");
    expect(resolveScreenKey("tailnet-trust", LIVE_KEYS)).toBe("devices");
    expect(resolveScreenKey("settings-sync", LIVE_KEYS)).toBe("devices");
    // The token stayed.
    expect(resolveScreenKey("security", LIVE_KEYS)).toBe("security");
  });

  it("a live key passes through; an unknown one is empty (the dialog falls back to General)", () => {
    expect(resolveScreenKey("devices", LIVE_KEYS)).toBe("devices");
    expect(resolveScreenKey("no-such-screen", LIVE_KEYS)).toBe("");
    expect(resolveScreenKey(null, LIVE_KEYS)).toBe("");
  });
});

describe("the nav and the palette use the new names", () => {
  it("nav labels", () => {
    const byKey = Object.fromEntries(LIVE.map((s) => [s.key, s.label]));
    expect(byKey.devices).toBe("Your devices");
    expect(byKey.mobile).toBe("Your phone");
    expect(byKey.peer).toBe("People");
    expect(byKey.security).toBe("Security");
  });

  it("the three sit in one group, Security stays under This device", () => {
    const group = (key: string) => NAV.match(new RegExp('key: "' + key + '"[^\\n]*group: "([^"]+)"'))?.[1];
    expect(group("devices")).toBe("Devices and people");
    expect(group("mobile")).toBe("Devices and people");
    expect(group("peer")).toBe("Devices and people");
    expect(group("security")).toBe("This device");
  });

  it("Voice sits right after Your phone, in the basic nav (no `advanced`)", () => {
    const i = (key: string) => LIVE_KEYS.indexOf(key);
    expect(i("voice")).toBe(i("mobile") + 1);
    expect(NAV).toMatch(/\{ key: "voice", label: "Voice", group: "Devices and people", el: /);
    expect(resolveScreenKey("mobile-voice", LIVE_KEYS)).toBe("voice");
    // Your phone no longer renders the voice settings, only the pointer.
    expect(SRC.mobile).not.toContain('id="voice-engines"');
    expect(SCREEN_SRC["Voice.tsx"]).toContain('id="voice-engines"');
  });

  it("Your phone is never hidden behind the Tailscale caps gate", () => {
    expect(NAV).not.toMatch(/"mobile" \? "tailscale"/);
  });

  it("palette entries", () => {
    expect(PALETTE).toMatch(/label: "Your devices"[\s\S]{0,200}openSettings\("devices"\)/);
    expect(PALETTE).toMatch(/label: "Your phone"[\s\S]{0,200}openSettings\("mobile"\)/);
    expect(PALETTE).toMatch(/label: "People…"[\s\S]{0,200}openSettings\("peer"\)/);
    expect(PALETTE).not.toContain("Work with someone");
  });
});

describe("each trust control is rendered by exactly one screen", () => {
  const ONCE: Array<[string, RegExp, keyof typeof FOUR]> = [
    ["the access-token display", /data-control="access-token"/, "security"],
    ["the Tailscale-mode switch", /data-control="tailscale-mode"/, "devices"],
    ["the trusted-accounts list", /data-control="trusted-accounts"/, "devices"],
  ];
  for (const [what, marker, owner] of ONCE) {
    it(what + " → " + owner, () => {
      const where = (Object.keys(FOUR) as Array<keyof typeof FOUR>).filter((k) => marker.test(SRC[k]));
      expect(where).toEqual([owner]);
      expect(SRC[owner].match(new RegExp(marker.source, "g"))?.length).toBe(1);
    });
  }

  it("the markers are on the real controls, not just comments", () => {
    expect(SRC.security).toMatch(/data-control="access-token"[\s\S]{0,400}id="auth-token-value"/);
    expect(SRC.devices).toMatch(/data-control="tailscale-mode"[\s\S]{0,900}data-field="serve_mode"/);
    expect(SRC.devices).toMatch(/data-control="trusted-accounts"[\s\S]{0,1500}data-login=\{login\}/);
  });

  it("the moved controls left their old screens", () => {
    // Your phone shows no token and owns no serve_mode save.
    expect(SRC.mobile).not.toMatch(/token-reveal|TokenField|serve_mode: /);
    // Security no longer lists trusted accounts, remote control or token pairing.
    expect(SRC.security).not.toMatch(/tailnet_trusted_logins|remote_control|\/connect`/);
    // People carries none of them.
    expect(SRC.peer).not.toMatch(/data-control=/);
    // The legacy pair and remote control live under Your devices' Advanced fold.
    expect(SRC.devices).toMatch(/id="devices-advanced"/);
    expect(SRC.devices).toMatch(/data-field="remote_control"/);
    expect(SRC.devices).toMatch(/\/connect`/);
  });

  it("Security points at Your devices and Your phone points at Make reachable", () => {
    expect(SRC.security).toMatch(/Trusting your own devices lives in[\s\S]{0,200}gotoScreen\("devices"\)/);
    expect(SRC.mobile).toMatch(/gotoScreen\("devices"\)\}>\s*Turn on Make reachable…/);
  });
});

describe("each ticket setting Intake owns is drawn on exactly one screen", () => {
  /** Every Settings screen module plus every Intake module: the screens a
   * control could be drawn on. */
  const INTAKE_SRC: Record<string, string> = Object.fromEntries(
    Object.entries(
      import.meta.glob<string>("../components/intake/*.tsx", { query: "?raw", import: "default", eager: true })
    ).map(([path, text]) => ["intake/" + path.replace(/^.*\//, ""), text])
  );
  const ALL: Record<string, string> = {
    ...Object.fromEntries(Object.entries(SCREEN_SRC).map(([f, t]) => ["settings/" + f, t])),
    ...INTAKE_SRC,
  };
  const drawnIn = (marker: RegExp) => Object.keys(ALL).filter((f) => marker.test(ALL[f]));

  const ONCE: Array<[string, RegExp, string]> = [
    ["engine.max_sessions", /group="engine"\s+field="max_sessions"/, "intake/QueueTab.tsx"],
    ["engine.mode (worktree or clone)", /group="engine"\s+field="mode"/, "intake/AutoStartSwitches.tsx"],
    ["engine.enabled (Ticket sessions in MindFlock)", /saveField\("engine", "enabled"/, "intake/AutoStartSwitches.tsx"],
    ["Open IDE when a ticket starts", /"\/api\/ide\/open-on-ticket", \{ json/, "intake/AutoStartSwitches.tsx"],
  ];
  for (const [what, marker, owner] of ONCE) {
    it(what + " → " + owner, () => {
      expect(drawnIn(marker)).toEqual([owner]);
      expect(ALL[owner].match(new RegExp(marker.source, "g"))?.length).toBe(1);
    });
  }

  const LINKED: Array<[string, string, string]> = [
    ["CodingCli.tsx", "coding-ticket-sessions-at-once", "Ticket sessions at once"],
    ["Workspace.tsx", "workspace-copy-mode", "How ticket sessions copy the repo"],
    ["Ide.tsx", "ide-ticket-row", "Open IDE when a ticket starts"],
    ["Advanced.tsx", "engine-ticket-sessions", "Ticket sessions in MindFlock"],
  ];
  it.each(LINKED)("%s keeps a Set in Intake › row for %s", (file, id, label) => {
    expect(SCREEN_SRC[file]).toMatch(
      new RegExp('<SetInIntake\\s+id="' + id + '"\\s+label="' + label + '"')
    );
  });

  it("the link opens Intake (on Auto-start) and reveals the control", () => {
    const link = SCREEN_SRC["SetInIntake.tsx"];
    expect(link).toContain('useUi.getState().openDialogFor("intake", tab);');
    expect(link).toContain('tab = "autostart"');
    expect(link).toContain("Set in Intake ›");
    // Every selector a row names exists in the Intake modules.
    for (const m of Object.values(SCREEN_SRC).join("\n").matchAll(/selector="#([a-z-]+)"/g))
      expect(Object.values(INTAKE_SRC).join("\n"), m[1]).toContain('id="' + m[1] + '"');
  });

  it("settings search still finds them by their old words, on the linking row", () => {
    const top = (q: string) => searchSettings(q)[0];
    expect(top("max sessions")?.anchor).toBe("#coding-sessions-at-once");
    expect(top("ticket sessions at once")?.anchor).toBe("#coding-sessions-at-once");
    expect(top("copy the repo")?.anchor).toBe("#workspace-copy-mode");
    expect(top("open ide when a ticket starts")?.anchor).toBe("#ide-ticket-row");
    expect(top("ticket sessions in mindflock")?.anchor).toBe("#engine-ticket-sessions");
    expect(top("pr review and issue handling")?.anchor).toBe("#devices-run-here");
    expect(top("live branch")?.label).toBe("Workspace → Default live branch");
  });

  it("the global live branch reads as the default; the device row says what it decides", () => {
    expect(SCREEN_SRC["Workspace.tsx"]).toContain('<span className="set-label">Default live branch</span>');
    expect(SCREEN_SRC["Workspace.tsx"]).not.toContain('<span className="set-label">Live branch</span>');
    expect(SCREEN_SRC["Devices.tsx"]).toContain('<span className="set-label">Which computer auto-starts work</span>');
  });
});
