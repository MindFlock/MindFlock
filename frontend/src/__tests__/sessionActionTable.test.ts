/** The session action table (lib/sessionActionTable): one row per verb, read
 * by the sidebar › menu, the palette and the Ctrl+K chords.
 *
 *  - every chord is a table row, with the words the "?" sheet always showed
 *  - a chord refuses (with a toast) what the menu and palette leave out
 *  - Merge PR is offered only once there is a PR
 *  - openSettings takes only a real screen key, and nothing else opens Settings */

/// <reference types="vite/client" />
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Caps, Instance } from "../api/types";

const commitSession = vi.fn();
const toast = vi.fn();
vi.mock("../lib/sessionActions", async (orig) => ({
  ...((await orig()) as object),
  commitSession: (...a: unknown[]) => commitSession(...a),
}));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));

const { CHORDS } = await import("../lib/keymap");
const { SESSION_ACTIONS, CHORD_ACTIONS, actionCtx, actionHint, actionLabel, runChordAction, sessionAction } = await import(
  "../lib/sessionActionTable"
);
const { queryClient } = await import("../state/queries");
const { useUi } = await import("../state/store");
const { openSettings } = await import("../lib/openSettings");
import type { SettingsScreen } from "../lib/openSettings";

const CAPS: Caps = { git: true, tailscale: true, ticketing: true, github: true };
const ctx = (o: Partial<Instance>, caps: Caps = CAPS) =>
  actionCtx("s", { inst: { title: "s", status: "ready", ...o } as Instance, caps, ideName: "Cursor", hidden: false });

afterEach(() => {
  commitSession.mockReset();
  toast.mockReset();
  queryClient.setQueryData(["instances"], []);
});

describe("the chords are table rows", () => {
  it("every chord letter comes from the table, with the sheet's words", () => {
    // …plus N, the one chord that is not a session verb (lib/needsYou).
    expect(Object.keys(CHORDS)).toEqual([...CHORD_ACTIONS.map((id) => sessionAction(id).chord), "n"]);
    expect(Object.fromEntries(Object.entries(CHORDS).map(([k, c]) => [k, c.desc]))).toEqual({
      c: "Commit…",
      p: "Push",
      r: "Open PR",
      o: "Open in your code editor",
      d: "Duplicate session",
      b: "Branch session",
      h: "Hide / show window",
      m: "Code map",
      s: "Message…",
      f: "Fast-track…",
      t: "Thread — workers and messages",
      n: "Next session that needs you — in the bell's order",
    });
  });

  it("each chord's shortcut text names its own letter", () => {
    for (const id of CHORD_ACTIONS) {
      const a = sessionAction(id);
      expect(a.keys).toBe("Ctrl+K " + a.chord!.toUpperCase());
    }
  });

  it("a chord says why it doesn't apply instead of firing a refused call", () => {
    queryClient.setQueryData(["instances"], [{ title: "shared", status: "ready", peer_share: true }]);
    runChordAction("commit", "shared");
    expect(commitSession).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledWith(expect.stringContaining("Commit… isn’t available"));
    queryClient.setQueryData(["instances"], [{ title: "mine", status: "ready" }]);
    CHORDS.c.run("mine");
    expect(commitSession).toHaveBeenCalledWith("mine");
  });
});

describe("enable rules and words", () => {
  it("Merge PR only once there is a PR", () => {
    expect(SESSION_ACTIONS.merge.enabled(ctx({ stage: "pushed" }))).toBe(false);
    expect(SESSION_ACTIONS.merge.enabled(ctx({ stage: "pr" }))).toBe(true);
    expect(SESSION_ACTIONS.merge.enabled(ctx({ stage: "pr", peer_share: true }))).toBe(false);
  });

  it("a shared folder gets no git verbs, rename, copies or IDE — but can still close", () => {
    const c = ctx({ peer_share: true });
    for (const id of ["commit", "push", "pr", "rename", "duplicate", "ide", "delete"] as const)
      expect(sessionAction(id).enabled(c)).toBe(false);
    expect(sessionAction("close").enabled(c)).toBe(true);
  });

  it("without gh or a token, the PR verbs still run and say where they end", () => {
    const c = ctx({ stage: "pr" }, { ...CAPS, github: false });
    expect(SESSION_ACTIONS.pr.enabled(c)).toBe(true);
    expect(SESSION_ACTIONS.pr.external!(c)).toBe(true);
    expect(actionHint("pr", c)).toBe("Ctrl+K R · opens GitHub");
    expect(actionHint("merge", c)).toBe("opens GitHub");
    expect(actionHint("pr", ctx({}))).toBe("Ctrl+K R");
  });

  it("the window verb follows the pane, the IDE verb names the IDE", () => {
    expect(actionLabel("hide", { ...ctx({}), hidden: true })).toBe("Show window");
    expect(actionLabel("hide", ctx({}))).toBe("Hide window");
    expect(actionLabel("ide", ctx({}))).toBe("Open in Cursor");
  });

  it("Close's palette hint carries its shortcut and where the session goes", () => {
    expect(actionHint("close", ctx({}))).toBe("Ctrl+W · stops the agent and moves it to Recently closed");
    expect(actionHint("pause", ctx({}))).toBe("stops the agent; keeps it in the sidebar to resume");
  });
});

describe("openSettings", () => {
  it("opens Settings on the named screen", () => {
    openSettings("devices");
    expect(useUi.getState().openDialog).toBe("settings");
    expect(useUi.getState().dialogTarget).toBe("devices");
    // No screen named: no deep link — the dialog itself opens on the screen
    // you last had open this page life, else Connections, the first-hour
    // status page (r5 settings #4; lib/settingsLastScreen openingScreen).
    openSettings();
    expect(useUi.getState().dialogTarget).toBeNull();
    useUi.getState().closeDialog();
  });

  it("a misspelled screen key is a type error (checked by tsc in the build)", () => {
    // @ts-expect-error — "devcies" is no Settings screen
    const typo: SettingsScreen = "devcies";
    const ok: SettingsScreen = "peer";
    expect([typo, ok]).toHaveLength(2);
  });

  it("is the only way the app opens Settings (so every key is type-checked)", () => {
    const src = import.meta.glob<string>(["../**/*.{ts,tsx}", "!../__tests__/**", "!../lib/openSettings.ts"], {
      query: "?raw",
      import: "default",
      eager: true,
    });
    expect(Object.keys(src).length).toBeGreaterThan(50);
    const raw = Object.entries(src)
      .filter(([, text]) => /openDialogFor\(\s*["']settings["']/.test(text))
      .map(([path]) => path);
    expect(raw).toEqual([]);
  });
});
