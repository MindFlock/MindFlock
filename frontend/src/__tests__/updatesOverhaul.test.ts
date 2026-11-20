/** The updates overhaul, web UI half (docs/web-ui.md "Updates"): the one
 * setting's words, the one line of state, who installs, the update.* events
 * as toasts and bell rows, the calm connection card while the server
 * restarts onto a new version, and Settings search finding the block. Every
 * new server field is optional: an older server's answer must read as
 * before. */

import { afterEach, describe, expect, it } from "vitest";
import {
  canOfferUpdate,
  doneToastWanted,
  DONE_TOAST_FRESH_S,
  installPath,
  restartLanded,
  runFailText,
  UPDATE_MODES,
  updateAvailable,
  updateButtonLabel,
  updateEventNote,
  updateModeOf,
  updateStateLine,
  vLabel,
  whatsNewCovers,
} from "../lib/updates";
import {
  _resetUpdateRestart,
  clearUpdateRestart,
  EXPECT_MAX_MS,
  expectUpdateRestart,
  installUpdateRecovery,
  onUpdateDone,
  updateRestartExpected,
} from "../lib/updateRestart";
import { updateRestartText } from "../components/ConnBanner";
import { notifFromEvent } from "../components/NotificationsBell";
import { searchSettings } from "../components/palette/settingsSearch";
import type { EventEnvelope } from "../state/queries";
import { code, squash } from "./sourceText";

afterEach(() => _resetUpdateRestart());

describe("the setting", () => {
  it("is read from the check, and absent on an older server", () => {
    expect(updateModeOf({ mode: "auto" })).toBe("auto");
    expect(updateModeOf({ mode: "ask" })).toBe("ask");
    expect(updateModeOf({ mode: "off" })).toBe("off");
    expect(updateModeOf({})).toBeNull();
    expect(updateModeOf({ mode: "sometimes" })).toBeNull();
    expect(updateModeOf(null)).toBeNull();
  });
  it("offers three plain choices", () => {
    expect(UPDATE_MODES.map((m) => m.label)).toEqual(["Automatic", "Ask me", "Off"]);
    for (const m of UPDATE_MODES) expect(m.hint).not.toMatch(/git|editable/i);
  });
});

describe("the one line of state", () => {
  it("a checkout that can't update says why; otherwise what automatic does", () => {
    const why = "This is a development checkout on branch foo. Updates apply when it is on main with nothing uncommitted.";
    expect(updateStateLine({ dev: { can_update: false, why_not: why }, auto: { why: "x" } })).toBe(why);
    expect(updateStateLine({ dev: { can_update: true }, auto: { why: "Installs by itself and restarts when you pause." } })).toBe(
      "Installs by itself and restarts when you pause."
    );
    expect(updateStateLine({ dev: null })).toBe("");
    expect(updateStateLine({})).toBe("");
    expect(updateStateLine(null)).toBe("");
  });
});

describe("what there is to install", () => {
  it("a release, unless blocked", () => {
    expect(updateAvailable({ available: true })).toBe(true);
    expect(updateAvailable({ available: true, blocked: "pipx is not on PATH" })).toBe(false);
    expect(updateAvailable({ available: false })).toBe(false);
    expect(updateAvailable(null)).toBe(false);
  });
  it("a checkout: behind and able to fast-forward gets the same button", () => {
    const dev = { branch: "main", default_branch: "main", behind: 3, ahead: 0, dirty: false, can_update: true };
    expect(updateAvailable({ available: true, dev })).toBe(true);
    // An older server blocks every checkout; a new one clears `blocked`.
    expect(updateAvailable({ available: true, dev, blocked: "" })).toBe(true);
    expect(updateAvailable({ available: true, dev: { ...dev, can_update: false } })).toBe(false);
  });
  it("names the version, or the commits when a checkout's are not a release", () => {
    expect(updateButtonLabel({ available: true, latest: "0.14.22" })).toBe("Update to v0.14.22");
    expect(updateButtonLabel({ available: true, latest: "0.14.22", current: "0.14.21", dev: { behind: 3, can_update: true } })).toBe(
      "Update to v0.14.22"
    );
    expect(updateButtonLabel({ available: true, current: "0.14.21", dev: { behind: 1, can_update: true } })).toBe(
      "Update (1 new commit)"
    );
    expect(updateButtonLabel({ available: true, latest: "0.14.21", current: "0.14.21", dev: { behind: 4, can_update: true } })).toBe(
      "Update (4 new commits)"
    );
    expect(updateButtonLabel(null)).toBe("Update");
  });
  it("vLabel", () => {
    expect(vLabel("0.14.22")).toBe("v0.14.22");
    expect(vLabel("v0.14.22")).toBe("v0.14.22");
    expect(vLabel("")).toBe("");
    expect(vLabel(undefined)).toBe("");
  });
});

describe("who installs", () => {
  it("the server everywhere but a desktop app whose shell manages the engine", () => {
    expect(installPath({ desktop: false })).toBe("server");
    expect(installPath({ desktop: false, managedByShell: true })).toBe("server");
    expect(installPath({ desktop: true, managedByShell: true })).toBe("shell");
    expect(installPath({ desktop: true, managedByShell: false })).toBe("server");
  });
  it("an older server (no managed_by_shell): today's split", () => {
    expect(installPath({ desktop: true })).toBe("shell");
    expect(installPath({ desktop: true, shellSource: true })).toBe("server");
  });
});

describe("the restart", () => {
  it("has landed once nothing waits and nothing installs", () => {
    expect(restartLanded({ restart_pending: false, state: "done" })).toBe(true);
    expect(restartLanded({})).toBe(true);
    expect(restartLanded({ restart_pending: true, state: "done" })).toBe(false);
    expect(restartLanded({ state: "started" })).toBe(false);
    expect(restartLanded(null)).toBe(false);
  });
  it("is expected from an install's start until done, failed, or 45 minutes", () => {
    expect(updateRestartExpected()).toBeNull();
    expectUpdateRestart("v0.14.22", 1000);
    expect(updateRestartExpected(1000 + 60_000)).toEqual({ version: "0.14.22", since: 1000 });
    expect(updateRestartExpected(1000 + EXPECT_MAX_MS + 1)).toBeNull();
    clearUpdateRestart();
    expect(updateRestartExpected(1001)).toBeNull();
  });
  it("update.done reloads only a live page that went through the restart", () => {
    // A page that loaded after the restart (a replayed installing): no reload.
    expectUpdateRestart("0.14.22");
    expect(onUpdateDone(true)).toBe(false);
    expect(updateRestartExpected()).toBeNull();
    // A replay never reloads either.
    expectUpdateRestart("0.14.22");
    expect(onUpdateDone(false)).toBe(false);
    // Nothing expected: nothing to do.
    expect(onUpdateDone(true)).toBe(false);
  });
  it("a drop while expected starts the watch; a drop otherwise doesn't", () => {
    let cb: ((s: "connected" | "disconnected") => void) | null = null;
    const off = installUpdateRecovery({
      onStatus: (f) => {
        cb = f;
        return () => (cb = null);
      },
    });
    expect(cb).not.toBeNull();
    cb!("disconnected"); // nothing expected: no watch, no error
    off();
    expect(cb).toBeNull();
  });
  it("the connection card says it calmly", () => {
    const t = updateRestartText("0.14.22");
    expect(t.head).toBe("Updating MindFlock");
    expect(t.text).toContain("restarting onto v0.14.22");
    expect(t.text).toContain("reconnects on its own");
    expect(t.head + t.text).not.toMatch(/lost|stopped|⚠/);
    expect(updateRestartText("").text).toContain("the new version");
  });
  it("a failed run says what happened, never a code alone", () => {
    expect(runFailText({ state: "rolled_back" })).toMatch(/previous one was put back/);
    expect(runFailText({ state: "failed", error: "interrupted" })).toMatch(/interrupted/);
    expect(runFailText({ state: "failed", error: "The checkout has uncommitted changes" })).toBe(
      "The checkout has uncommitted changes."
    );
    expect(runFailText({ state: "failed", code: 2 })).toMatch(/exit 2/);
  });
});

describe("the update.* events", () => {
  it("installing, done and failed in plain words", () => {
    expect(updateEventNote("update.installing", { version: "0.14.22", auto: true })).toEqual({
      text: "",
      toast: "Updating to v0.14.22…",
      cls: "n-info",
      version: "0.14.22",
    });
    expect(updateEventNote("update.done", { version: "0.14.22", from_version: "0.14.21" })?.text).toBe("Updated to v0.14.22");
    expect(updateEventNote("update.failed", { version: "0.14.22", error: "The new version didn't start." })?.text).toBe(
      "Couldn’t update to v0.14.22: The new version didn't start."
    );
    expect(updateEventNote("update.failed", { version: "0.14.22" })?.cls).toBe("n-warn");
    expect(updateEventNote("update.available", { latest: "0.14.22" })).toBeNull();
  });
  it("Update is offered to the owner, for this device, when nothing blocks it", () => {
    const d = { latest: "0.14.22", here: true, count: 1, behind: [] };
    expect(canOfferUpdate(d, true)).toBe(true);
    expect(canOfferUpdate(d, false)).toBe(false); // a guest
    expect(canOfferUpdate({ ...d, auto: true }, true)).toBe(false);
    expect(canOfferUpdate({ ...d, here: false }, true)).toBe(false);
    expect(canOfferUpdate({ ...d, blocked: "uv is not on PATH" }, true)).toBe(false);
    expect(canOfferUpdate({}, true)).toBe(false);
    // An older server says neither `auto` nor `here` on some answers.
    expect(canOfferUpdate({ latest: "0.14.22" }, true)).toBe(true);
  });
  it("Updated toasts once: live, or replayed onto the reloaded page while fresh", () => {
    const base = { version: "0.14.22", ts: 1000, nowS: 1010, seen: null };
    expect(doneToastWanted({ ...base, replay: false })).toBe(true);
    expect(doneToastWanted({ ...base, replay: true })).toBe(true);
    expect(doneToastWanted({ ...base, replay: true, nowS: 1000 + DONE_TOAST_FRESH_S + 1 })).toBe(false);
    expect(doneToastWanted({ ...base, replay: false, seen: "0.14.22" })).toBe(false);
    expect(doneToastWanted({ ...base, replay: false, seen: "0.14.21" })).toBe(true);
  });
  it("What's new is offered when it has entries for the update", () => {
    const rel = [{ version: "0.14.22" }, { version: "0.14.0" }];
    expect(whatsNewCovers(rel, "0.14.22", "0.14.21")).toBe(true);
    expect(whatsNewCovers(rel, "0.14.21", "0.14.20")).toBe(false);
    expect(whatsNewCovers(rel, "0.14.5", "")).toBe(true);
    expect(whatsNewCovers(rel, "", "0.14.0")).toBe(false);
  });
});

const env = (e: Partial<EventEnvelope>): EventEnvelope => ({
  event: "update.done",
  session: "",
  seq: 1,
  ts: 100,
  old: null,
  new: null,
  data: {},
  ...e,
});

describe("bell rows", () => {
  it("done and failed are Updates rows; installing has none", () => {
    expect(notifFromEvent(env({ data: { version: "0.14.22", from_version: "0.14.21" } }))).toMatchObject({
      text: "Updated to v0.14.22",
      cls: "n-done",
      settings: "advanced",
      dedupe: "update-done:0.14.22",
    });
    expect(notifFromEvent(env({ event: "update.failed", data: { version: "0.14.22", error: "Network is down" } }))).toMatchObject({
      text: "Couldn’t update to v0.14.22: Network is down.",
      cls: "n-warn",
      settings: "advanced",
    });
    expect(notifFromEvent(env({ event: "update.installing", data: { version: "0.14.22" } }))).toBeNull();
  });
  it("available, this device only: carries Update (the owner's page in tests)", () => {
    const row = notifFromEvent(env({ event: "update.available", data: { latest: "0.14.22", here: true, count: 1, behind: [] } }));
    expect(row).toMatchObject({ text: "MindFlock v0.14.22 is out", settings: "advanced", update: "0.14.22" });
  });
  it("available, other devices behind: the fleet wording, no Update here", () => {
    const d = {
      latest: "0.14.22",
      here: true,
      count: 2,
      behind: [{ key: "rig", host: "Rig", version: "0.14.20" }],
      detail: "MindFlock v0.14.22 is out — 2 of your devices are behind",
    };
    const row = notifFromEvent(env({ event: "update.available", data: d }));
    expect(row).toEqual({ text: d.detail, cls: "n-info", settings: "devices", dedupe: "update:0.14.22:2" });
  });
});

describe("Settings search", () => {
  it("finds the Updates block under its words", () => {
    for (const q of ["update", "automatic updates", "version", "auto update"]) {
      const hits = searchSettings(q);
      expect(hits[0]?.anchor, q).toBe("#set-updates");
      expect(hits[0]?.key).toBe("advanced");
    }
  });
  it("lands on the block's heading", () => {
    expect(squash(code("components/settings/screens/Advanced.tsx"))).toContain('id="set-updates"');
  });
});

describe("wiring", () => {
  const TOASTS = squash(code("components/EventToasts.tsx"));
  it("each update event is subscribed, and the recovery is installed", () => {
    for (const name of ["update.available", "update.installing", "update.done", "update.failed"])
      expect(TOASTS).toContain('ev.subscribe("' + name + '"');
    expect(TOASTS).toContain("unsubs.push(installUpdateRecovery(ev));");
  });
  it("in the desktop app, Updated is left to the shell where it drives", () => {
    expect(TOASTS).toContain("if (c?.managed_by_shell !== true) say();");
  });
  it("in the desktop app, update.available toasts for a dev engine (no shell toast there)", () => {
    expect(TOASTS).toContain("if (c?.managed_by_shell === false) show();");
  });
  it("update.available's toast installs from right there", () => {
    expect(TOASTS).toContain('action: { label: "Update", onClick: () => void startUpdateHere(latest) }');
  });
  it("the connection card is calm while the restart is expected", () => {
    const banner = squash(code("components/ConnBanner.tsx"));
    expect(banner).toContain("const updating = useUpdateRestart();");
    expect(banner).toContain('role={updating ? "status" : "alert"}');
  });
});
