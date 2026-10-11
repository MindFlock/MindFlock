/** Settings → Advanced on the desktop shell: a run from source never offers
 * an update (it says "update with git"), and the automatic-updates switch is
 * shown only by shells that report it. */

import { describe, expect, it } from "vitest";
import {
  autoUpdateHint,
  shellUpdateMode,
  showAutoUpdateSwitch,
  SOURCE_RUN_NOTE,
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
    expect(SOURCE_RUN_NOTE).toMatch(/running from source/);
    expect(SOURCE_RUN_NOTE).toMatch(/git/);
  });
});

describe("Advanced.tsx wiring", () => {
  it("a source run renders the note and the engine's own view, no shell button", () => {
    expect(ADVANCED).toContain('shellUpdateMode(info) === "source"');
    const branch = ADVANCED.slice(ADVANCED.indexOf('shellUpdateMode(info) === "source"'));
    const src = branch.slice(0, branch.indexOf("\n  }\n")); // the if-block
    expect(src).toContain("return (");
    expect(src).toContain("SOURCE_RUN_NOTE");
    expect(src).toContain("<ServerEngineUpdate />");
    expect(src).not.toContain("mfengine.install");
  });
  it("the switch saves through the shell bridge", () => {
    expect(ADVANCED).toContain("showAutoUpdateSwitch(info)");
    expect(ADVANCED).toContain("mfupdate?.setAuto");
    expect(ADVANCED).toContain("Install updates automatically");
  });
});
