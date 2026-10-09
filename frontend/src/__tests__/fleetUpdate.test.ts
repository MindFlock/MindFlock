/** "Update all my devices" (lib/fleet.ts): the per-member chips, the line
 * over the Update-all button, the rollout's wording, and the update.available
 * note the bell and the toast show — plus the bell row it becomes. */

import { describe, it, expect } from "vitest";
import {
  cmpVersion,
  devicesBehind,
  memberStatus,
  memberUpdateBlocker,
  memberUpdateChips,
  rolloutLine,
  rolloutRowText,
  updateAllLine,
  updateNote,
  updateToastWanted,
} from "../lib/fleet";
import { notifFromEvent } from "../components/NotificationsBell";
import type { FleetMember, FleetRollout } from "../api/types";
import type { EventEnvelope } from "../state/queries";

const member = (m: Partial<FleetMember>): FleetMember => ({
  key: "mini",
  host: "mac-mini",
  added_at: 1,
  self: false,
  reachable: true,
  version: "0.7.4",
  same_fleet: true,
  error: "",
  install: "uv-tool",
  commit: "",
  shell_version: "",
  ...m,
});

describe("cmpVersion", () => {
  it("compares numerically, ignoring a leading v", () => {
    expect(cmpVersion("0.10.0", "0.9.9")).toBeGreaterThan(0);
    expect(cmpVersion("v0.7.4", "0.7.4")).toBe(0);
    expect(cmpVersion("0.7.4", "0.8")).toBeLessThan(0);
  });
});

describe("member chips", () => {
  it("says a member is behind the newest release", () => {
    expect(memberUpdateChips(member({}), "0.8.0")).toEqual([{ text: "v0.8.0 available", warn: true }]);
    expect(memberUpdateChips(member({ version: "0.8.0" }), "0.8.0")).toEqual([]);
    // Offline: its version is a memory, not a fact.
    expect(memberUpdateChips(member({ reachable: false }), "0.8.0")).toEqual([]);
  });

  it("says why one can't be updated from here instead", () => {
    expect(memberUpdateBlocker(member({ install: "editable" }))).toMatch(/dev checkout/);
    expect(memberUpdateChips(member({ install: "other" }), "0.8.0")).toEqual([
      { text: "not installed by install.sh — update it there", warn: false },
    ]);
  });

  it("says a desktop app behind its engine updates on its next launch", () => {
    expect(memberUpdateChips(member({ version: "0.8.0", shell_version: "0.7.4" }), "0.8.0")).toEqual([
      { text: "desktop app v0.7.4 updates on its next launch", warn: false },
    ]);
  });
});

describe("memberStatus with commits", () => {
  it("tells two builds of one version apart", () => {
    expect(memberStatus(member({ commit: "aaa" }), "0.7.4", "bbb")).toBe(
      "runs a different build of 0.7.4; update both to the same release"
    );
    expect(memberStatus(member({ commit: "aaa" }), "0.7.4", "aaa")).toBe("online");
    // Unknown on either side is not a mismatch.
    expect(memberStatus(member({ commit: "" }), "0.7.4", "bbb")).toBe("online");
  });
});

describe("the Update-all line", () => {
  const members = [
    member({ key: "me", self: true, version: "0.7.4" }),
    member({ key: "rig", version: "0.6.1" }),
    member({ key: "dev", version: "0.7.4", install: "editable" }),
    member({ key: "new", version: "0.8.0" }),
  ];
  it("counts every device behind, this one included", () => {
    expect(devicesBehind(members, "0.8.0").map((m) => m.key)).toEqual(["me", "rig", "dev"]);
    expect(updateAllLine(members, "0.8.0")).toBe(
      "MindFlock v0.8.0 is out — 3 of your devices are behind. 1 can't be updated from here (see its row)."
    );
  });
  it("is empty when nothing is behind or the newest release is unknown", () => {
    expect(updateAllLine(members, "0.6.0")).toBe("");
    expect(updateAllLine(members, "")).toBe("");
  });
});

describe("the rollout", () => {
  const roll = (r: Partial<FleetRollout>): FleetRollout => ({
    state: "running",
    tag: "v0.8.0",
    version: "0.8.0",
    members: [],
    ...r,
  });
  it("says what it is doing, how it ended, and why it stopped", () => {
    expect(rolloutLine(roll({}))).toBe("Updating your devices to v0.8.0, one at a time…");
    expect(rolloutLine(roll({ state: "halted", error: "Rig: timed out" }))).toBe(
      "Stopped updating your devices: Rig: timed out."
    );
    expect(
      rolloutLine(roll({ state: "done", members: [{ key: "a", host: "A", step: "skipped" }] }))
    ).toBe("Your devices are on v0.8.0 — 1 skipped (see below).");
    expect(rolloutLine(roll({ state: "idle" }))).toBe("");
    expect(rolloutLine(undefined)).toBe("");
  });
  it("one line per device", () => {
    expect(rolloutRowText({ key: "rig", host: "Rig", step: "failed", detail: "rolled back" })).toBe(
      "Rig: failed — rolled back"
    );
    expect(rolloutRowText({ key: "me", host: "Laptop", self: true, step: "updating" })).toBe(
      "Laptop (this device): updating…"
    );
  });
});

const env = (e: Partial<EventEnvelope>): EventEnvelope => ({
  event: "update.available",
  session: "",
  seq: 1,
  ts: 0,
  old: null,
  new: null,
  data: {},
  ...e,
});

describe("update.available", () => {
  it("points at Devices when other devices are behind", () => {
    const d = {
      latest: "0.8.0",
      here: true,
      count: 2,
      behind: [{ key: "rig", host: "Rig", version: "0.6.1" }],
      detail: "MindFlock v0.8.0 is out — 2 of your devices are behind",
    };
    expect(updateNote(d)).toEqual({
      text: d.detail,
      screen: "devices",
      toast: d.detail + " — Update them in Settings → Devices",
    });
    expect(notifFromEvent(env({ data: d }))).toEqual({
      text: d.detail,
      cls: "n-info",
      settings: "devices",
      dedupe: "update:0.8.0:2",
    });
  });
  it("points at Advanced when only this device is behind", () => {
    const n = updateNote({ latest: "0.8.0", here: true, count: 1, behind: [] });
    expect(n?.screen).toBe("advanced");
    expect(n?.text).toBe("MindFlock v0.8.0 is out");
  });
  it("toasts in the desktop app only when other devices are behind", () => {
    const mine = { latest: "0.8.0", here: true, count: 1, behind: [] };
    const others = { ...mine, count: 2, behind: [{ key: "rig", host: "Rig", version: "0.7.4" }] };
    expect(updateToastWanted(mine, false)).toBe(true);
    expect(updateToastWanted(mine, true)).toBe(false); // the shell's own toast
    expect(updateToastWanted(others, true)).toBe(true);
    expect(updateToastWanted(null, true)).toBe(false);
  });
  it("ignores a payload without a version", () => {
    expect(updateNote({})).toBeNull();
    expect(notifFromEvent(env({ data: {} }))).toBeNull();
  });
});
