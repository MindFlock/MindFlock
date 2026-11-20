/** Settings → Advanced on the desktop shell: a run from source never installs
 * through the shell (the server does), and the shell's own automatic-updates
 * switch is drawn only by shells that report it, and only when the server is
 * too old to hold the setting (no `mode` in its check). */

import { describe, expect, it } from "vitest";
import {
  autoUpdateHint,
  shellUpdateMode,
  showAutoUpdateSwitch,
} from "../lib/shellUpdate";

const ADVANCED = Object.values(
  import.meta.glob<string>("../components/settings/screens/Advanced.tsx", {
    query: "?raw",
    import: "default",
    eager: true,
  })
)[0];

describe("shellUpdateMode", () => {
  it("a source run gets the git view, everything else the shell's updater", () => {
    expect(shellUpdateMode({ source: true })).toBe("source");
    expect(shellUpdateMode({ autoUpdate: true })).toBe("shell");
    expect(shellUpdateMode({})).toBe("shell");
    expect(shellUpdateMode(null)).toBe("shell");
  });
});

describe("showAutoUpdateSwitch", () => {
  it("only when the shell reports the setting, and never from source", () => {
    expect(showAutoUpdateSwitch({ autoUpdate: true })).toBe(true);
    expect(showAutoUpdateSwitch({ autoUpdate: false })).toBe(true);
    expect(showAutoUpdateSwitch({})).toBe(false); // an older shell
    expect(showAutoUpdateSwitch(null)).toBe(false);
    expect(showAutoUpdateSwitch({ source: true, autoUpdate: true })).toBe(false);
  });
});

describe("wording", () => {
  it("says what automatic means, and that off still offers updates", () => {
    expect(autoUpdateHint(true)).toMatch(/away for 10 minutes/);
    expect(autoUpdateHint(true)).toMatch(/Skip this version/);
    expect(autoUpdateHint(false)).toMatch(/only offered/);
  });
});

describe("Advanced.tsx wiring", () => {
  it("one Updates block for the browser and the desktop app, no git commands", () => {
    expect(ADVANCED).toContain("<UpdatesBlock />");
    expect(ADVANCED).not.toContain("ShellEngineUpdate");
    expect(ADVANCED).not.toContain("ServerEngineUpdate");
    expect(ADVANCED).not.toContain("editable_commands");
    expect(ADVANCED).not.toContain("SOURCE_RUN_NOTE");
  });
  it("a source run reaches the server's install, through installPath", () => {
    expect(ADVANCED).toContain('shellSource: shellUpdateMode(shellInfo) === "source"');
    expect(ADVANCED).toContain("managedByShell: info?.managed_by_shell");
    expect(ADVANCED).toContain('path === "shell" ? viaShell() : void viaServer()');
  });
  it("the choice saves to the server, then (awaited) tells the desktop app", () => {
    expect(ADVANCED).toContain('const r = await api<{ mode?: string }>("/api/update/mode", { json: { mode: next } });');
    expect(ADVANCED).toContain('await mfupdate?.setAuto?.(next === "auto")');
    // The shell's setAuto(false) posts "ask" itself: told first, it could
    // turn "off" into "ask".
    expect(ADVANCED.indexOf('"/api/update/mode"')).toBeLessThan(
      ADVANCED.indexOf('mfupdate?.setAuto?.(next === "auto")')
    );
  });
  it("the older shell switch stays for an older server", () => {
    expect(ADVANCED).toContain('const legacyAuto = !mode && path === "shell" && showAutoUpdateSwitch(shellInfo);');
    expect(ADVANCED).toContain("Install updates automatically");
  });
});
