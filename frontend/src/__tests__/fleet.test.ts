/** Settings → Devices helpers (lib/fleet.ts): code normalizing/formatting
 * (must agree with the server's normalize_code), the invite countdown, and
 * the one-line statuses and event notes the screen, the bell and the toasts
 * show. */

import { describe, it, expect, afterEach, vi } from "vitest";
import {
  ROTATE_TOKENS_LABEL,
  addPairedNote,
  SYNC_PAUSED_NOTE,
  admitToast,
  automationHint,
  automationLine,
  candidateBlocker,
  candidateNote,
  deviceEventNote,
  fmtCountdown,
  formatCode,
  joinLine,
  joinSettingsNote,
  joinableCandidates,
  keyConflicts,
  leftLines,
  liveInvite,
  memberStatus,
  normalizeCode,
  pasteJoinBody,
  pinChoices,
  plausibleCode,
  readmittedLines,
  removalLines,
  removeConfirmText,
  removedToast,
  rotatedToast,
  syncDeviceLine,
  syncLabel,
  unpinReplaces,
} from "../lib/fleet";
import type { FleetCandidate, FleetJoin, FleetMember, FleetStatus, SyncStatus } from "../api/types";

describe("invite codes", () => {
  it("normalizes like the server: case, separators, Crockford look-alikes", () => {
    expect(normalizeCode(" abcd-efgh ")).toBe("ABCDEFGH");
    expect(normalizeCode("ab cd ef gh")).toBe("ABCDEFGH");
    expect(normalizeCode("il0o-uU")).toBe("1100VV");
  });

  it("formats an 8-char code as XXXX-XXXX and leaves others alone", () => {
    expect(formatCode("k7m2p9qx")).toBe("K7M2-P9QX");
    expect(formatCode("k7m2")).toBe("K7M2");
  });

  it("only an 8-char Crockford code is plausible", () => {
    expect(plausibleCode("K7M2-P9QX")).toBe(true);
    expect(plausibleCode("k7m2 p9qx")).toBe(true);
    expect(plausibleCode("K7M2-P9Q")).toBe(false);
    // L folds to 1, so it's fine; '!' never is.
    expect(plausibleCode("L7M2-P9Q!")).toBe(false);
    expect(plausibleCode("")).toBe(false);
  });
});

describe("fmtCountdown / liveInvite", () => {
  it("counts down m:ss and stops at 0:00", () => {
    expect(fmtCountdown(1000 + 582, 1000)).toBe("9:42");
    expect(fmtCountdown(1000 + 5, 1000)).toBe("0:05");
    expect(fmtCountdown(990, 1000)).toBe("0:00");
  });

  it("shows the newest live invite, never an expired one", () => {
    const st = {
      invites: [
        { code: "AAAA-AAAA", expires_at: 1100, command: "a" },
        { code: "BBBB-BBBB", expires_at: 1500, command: "b" },
        { code: "CCCC-CCCC", expires_at: 900, command: "c" },
      ],
    } as FleetStatus;
    expect(liveInvite(st, 1000)?.code).toBe("BBBB-BBBB");
    expect(liveInvite(st, 1600)).toBeNull();
    expect(liveInvite(null, 1000)).toBeNull();
  });
});

const member = (m: Partial<FleetMember>): FleetMember => ({
  key: "mini",
  host: "mac-mini",
  added_at: 1,
  self: false,
  reachable: true,
  version: "0.7.3",
  same_fleet: true,
  error: "",
  ...m,
});

describe("memberStatus", () => {
  it("says what each state is", () => {
    expect(memberStatus(member({ self: true }), "0.7.3")).toBe("this device");
    expect(memberStatus(member({}), "0.7.3")).toBe("online");
    expect(memberStatus(member({ reachable: false }), "0.7.3")).toBe("offline");
    expect(memberStatus(member({ error: "403 remote control is off" }), "0.7.3")).toBe(
      "403 remote control is off"
    );
    expect(memberStatus(member({ same_fleet: false }), "0.7.3")).toMatch(/hasn't picked up/);
  });

  it("names a version mismatch as something to fix", () => {
    expect(memberStatus(member({ version: "0.7.1" }), "0.7.3")).toBe(
      "runs 0.7.1 — this one runs 0.7.3; update both to the same version"
    );
    // Unknown on either side is not a mismatch.
    expect(memberStatus(member({ version: "" }), "0.7.3")).toBe("online");
    expect(memberStatus(member({ version: "0.7.1" }), "")).toBe("online");
  });
});

const cand = (c: Partial<FleetCandidate>): FleetCandidate => ({
  device: "rig",
  host: "ml-rig",
  version: "0.7.3",
  fleet_proto: 1,
  reachable: true,
  member: false,
  in_fleet: false,
  same_fleet: false,
  has_token: false,
  ...c,
});

describe("candidates", () => {
  it("a member whose hello lags is never offered as a computer to join", () => {
    const lagging = cand({ device: "mini", host: "mac-mini", member: true, has_token: true });
    const other = cand({});
    expect(joinableCandidates([lagging, other])).toEqual([other]);
    // …and a bare pasted code doesn't count it as the one joinable device.
    expect(pasteJoinBody("K7M2P9QX", [lagging, other], null).body).toEqual({ text: "K7M2P9QX", device: "rig" });
    expect(pasteJoinBody("K7M2P9QX", [lagging], null).body).toBeNull();
    expect(pasteJoinBody("K7M2P9QX", [lagging], "mini").body).toBeNull();
  });

  it("an offline or too-old MindFlock can't join, and says why", () => {
    expect(candidateBlocker(cand({}))).toBe("");
    expect(candidateBlocker(cand({ reachable: false }))).toBe("offline");
    expect(candidateBlocker(cand({ fleet_proto: 0 }))).toBe("update MindFlock on ml-rig to add it");
  });

  it("notes version, another group and a pasted token", () => {
    expect(candidateNote(cand({}))).toBe("0.7.3");
    expect(candidateNote(cand({ in_fleet: true, has_token: true }))).toBe(
      "0.7.3 · already one of another set of devices · paired"
    );
  });
});

describe("joinLine", () => {
  const j = (state: FleetJoin["state"], extra: Partial<FleetJoin> = {}): FleetJoin => ({
    state,
    device: "mini",
    host: "mac-mini",
    code: "123 456",
    error: "",
    id: "abc",
    ...extra,
  });

  it("is the waiting line both screens' codes are compared against", () => {
    expect(joinLine(j("waiting"))).toBe("Waiting for approval on mac-mini — code 123 456");
  });

  it("covers every end state, and idle says nothing", () => {
    expect(joinLine(j("idle"))).toBe("");
    expect(joinLine(j("joining"))).toBe("Joining mac-mini…");
    expect(joinLine(j("joined"))).toBe("Joined mac-mini.");
    expect(joinLine(j("denied"))).toBe("mac-mini said no.");
    expect(joinLine(j("expired"))).toMatch(/expired — ask again/);
    expect(joinLine(j("error", { error: "update MindFlock on mac-mini first" }))).toBe(
      "update MindFlock on mac-mini first"
    );
    expect(joinLine(null)).toBe("");
  });
});

describe("settings sync rows", () => {
  afterEach(() => vi.useRealTimers());

  it("one line per device", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(100_000 * 1000));
    const d = { key: "mini", label: "mac-mini", syncing: true, last_sync: 100_000 - 30, error: "" };
    expect(syncDeviceLine(d)).toBe("in sync · 30s ago");
    expect(syncDeviceLine({ ...d, last_sync: null })).toBe("waiting for the first sync");
    expect(syncDeviceLine({ ...d, syncing: false })).toBe("sync is off there");
    expect(syncDeviceLine({ ...d, error: "401 — rejoin" })).toBe("401 — rejoin");
  });

  const sync = {
    pinned: ["prefs.keymap"],
    syncable: [
      { path: "prefs.keymap", label: "Keyboard shortcuts", group: "Preferences" },
      { path: "prefs.theme", label: "Theme", group: "Preferences" },
      { path: "ui.accent", label: "Accent colour", group: "Appearance" },
      { path: "store:templates", label: "Session templates", group: "" },
    ],
  } as unknown as SyncStatus;

  it("labels a pinned path, falling back to the path", () => {
    expect(syncLabel("prefs.keymap", sync)).toBe("Keyboard shortcuts");
    expect(syncLabel("x.unknown", sync)).toBe("x.unknown");
    // A unit-level pin: one entry of a keyed list, named by its base.
    expect(syncLabel("prefs.keymap#jira-main", sync)).toBe("Keyboard shortcuts: jira-main");
    expect(syncLabel("ticketing.sources#jira-main", sync)).toBe("ticketing.sources: jira-main");
  });

  it("offers everything not pinned yet, grouped in first-seen order", () => {
    expect(pinChoices(sync)).toEqual([
      { group: "Preferences", items: [{ path: "prefs.theme", label: "Theme" }] },
      { group: "Appearance", items: [{ path: "ui.accent", label: "Accent colour" }] },
      { group: "Other", items: [{ path: "store:templates", label: "Session templates" }] },
    ]);
    expect(pinChoices(null)).toEqual([]);
  });

  it("unpinning a source kept separate at join says it replaces this device's source and its token", () => {
    const st = {
      ...sync,
      joined_from: "ml-rig",
      devices: [{ key: "ml-rig", label: "rig", syncing: true, last_sync: null, error: "" }],
    } as unknown as SyncStatus;
    // The leader comes from `separate` when the server names it, else the
    // device this one joined from.
    expect(unpinReplaces("ticketing.sources#sc", { ...st, separate: { "ticketing.sources#sc": "mac-mini" } })).toBe(
      "This replaces this device's “sc” (and its token) with mac-mini's. To keep both, give one of them a different id in Settings → Tickets instead."
    );
    expect(unpinReplaces("ticketing.sources#sc", st)).toMatch(/^This replaces this device's “sc” \(and its token\) with rig's\./);
    expect(unpinReplaces("ticketing.sources#sc", null)).toMatch(/with the one your other devices have under that id\./);
    // Any other pin just takes the others' value again: no confirm.
    expect(unpinReplaces("prefs.keymap", st)).toBe("");
    expect(unpinReplaces("store:templates", st)).toBe("");
  });
});

describe("deviceEventNote", () => {
  it("a join request: the server's detail in the bell, host + code in the toast", () => {
    const n = deviceEventNote("device.join_requested", {
      device: "mini",
      host: "mac-mini",
      code: "123 456",
      detail: "mac-mini · code 123 456",
    });
    expect(n).toEqual({
      text: "mac-mini · code 123 456",
      cls: "n-warn",
      toast: "mac-mini wants to join your devices — code 123 456",
    });
  });

  it("joined toasts; removed is bell-only; others are not device events", () => {
    expect(deviceEventNote("device.joined", { device: "rig" })?.toast).toBe("rig joined your devices");
    const removed = deviceEventNote("device.removed", { host: "ml-rig" });
    expect(removed?.cls).toBe("n-info");
    expect(removed?.toast).toBe("");
    expect(deviceEventNote("session.created", {})).toBeNull();
  });

  it("settings sync pausing itself warns, toasts and points at Settings → Devices", () => {
    const n = deviceEventNote("settings.sync_paused", {});
    expect(n).toEqual({ text: SYNC_PAUSED_NOTE, cls: "n-warn", toast: SYNC_PAUSED_NOTE });
    expect(SYNC_PAUSED_NOTE).toMatch(/Settings → Devices/);
  });
});

describe("device events about THIS device", () => {
  it("the joiner's own join reads from here and does not toast (the screen already said so)", () => {
    const n = deviceEventNote("device.joined", {
      device: "laptop",
      host: "laptop",
      via: "macmini",
      detail: "joined mac-mini's devices",
    });
    expect(n).toEqual({ text: "This device joined mac-mini's devices", cls: "n-done", toast: "" });
    const added = deviceEventNote("device.joined", {
      device: "rig",
      host: "ml-rig",
      via: "laptop",
      detail: "added to your devices by laptop",
    });
    expect(added?.text).toBe("This device was added to your devices by laptop");
    expect(added?.toast).toBe("");
  });

  it("another device joining still toasts, and uses the server's detail in the bell", () => {
    const n = deviceEventNote("device.joined", { device: "rig", host: "ml-rig", detail: "ml-rig joined your devices" });
    expect(n).toEqual({ text: "ml-rig joined your devices", cls: "n-done", toast: "ml-rig joined your devices" });
  });

  it("removed uses the server's detail (this device's own removal included)", () => {
    expect(
      deviceEventNote("device.removed", { device: "laptop", host: "laptop", detail: "this device was removed from your devices" })?.text
    ).toBe("This device was removed from your devices");
    expect(deviceEventNote("device.removed", { host: "ml-rig" })?.text).toBe("ml-rig is no longer one of your devices");
  });
});

describe("the paste box", () => {
  const cand = (over: Partial<FleetCandidate>): FleetCandidate => ({
    device: "mini",
    host: "mac-mini",
    version: "0.7.3",
    fleet_proto: 1,
    reachable: true,
    member: false,
    in_fleet: false,
    same_fleet: false,
    has_token: false,
    ...over,
  });

  it("a whole command goes as text: the server reads the device from it", () => {
    expect(pasteJoinBody(" mindflock devices join mini K7M2-P9QX ", [], null)).toEqual({
      body: { text: "mindflock devices join mini K7M2-P9QX" },
      error: "",
    });
  });

  it("a bare code goes to the only computer that can be joined", () => {
    const r = pasteJoinBody("K7M2-P9QX", [cand({}), cand({ device: "old", fleet_proto: 0 })], null);
    expect(r).toEqual({ body: { text: "K7M2-P9QX", device: "mini" }, error: "" });
  });

  it("a bare code with several candidates goes to the one whose Enter-code row is open, else asks", () => {
    const two = [cand({}), cand({ device: "rig", host: "ml-rig" })];
    expect(pasteJoinBody("K7M2P9QX", two, "rig").body).toEqual({ text: "K7M2P9QX", device: "rig" });
    const r = pasteJoinBody("K7M2P9QX", two, null);
    expect(r.body).toBeNull();
    expect(r.error).toMatch(/Enter code/);
  });
});

describe("what the admitting and removing actions say", () => {
  it("a sync_error from approve / add-paired is shown, not swallowed", () => {
    expect(admitToast("mac-mini", "mac-mini is joining your devices", "")).toBe("mac-mini is joining your devices");
    expect(admitToast("mac-mini", "ok", "settings_sync.json: permission denied")).toBe(
      "mac-mini is one of your devices, but settings sync didn't start here: settings_sync.json: permission denied"
    );
  });

  it("the remove confirm says exactly what removal does, and offers to replace the access tokens", () => {
    const body = removeConfirmText("laptop");
    expect(body).toMatch(/^laptop stops getting settings sync, sign-in and ticket claims/);
    expect(body).toMatch(/new device key/);
    // An offline member is healed by the next contact, not sent to rejoin.
    expect(body).toMatch(/offline right now gets it when it's back/);
    expect(body).not.toMatch(/rejoin/);
    // What the shared key can't do, the tailnet can.
    expect(body).toMatch(
      /If laptop was lost or stolen, also remove it from your tailnet in the Tailscale admin console — that cuts it off everywhere at once, even from devices that are offline now\.$/
    );
    expect(ROTATE_TOKENS_LABEL).toBe(
      "Also replace every device's access token (do this if it was lost or stolen — your phone will need to scan the QR again)"
    );
  });

  it("the toast after removal reports offline devices and the token replacement", () => {
    const advice =
      ". If laptop was lost or stolen, also remove it from your tailnet in the Tailscale admin console — that cuts it off everywhere at once, even from devices that are offline now.";
    expect(removedToast("laptop", { missed: [], rotated: ["mini"], rotate_failed: [] }, true)).toBe(
      "Removed laptop — access tokens replaced" + advice
    );
    // The offline member gets the new key on its next contact (under the
    // old key, which the others keep) — no rejoin.
    expect(removedToast("laptop", { missed: ["rig"], rotate_failed: ["rig"] }, true)).toBe(
      "Removed laptop — rig was offline — it gets the new key when it's back — couldn't replace the access token on rig — do it there in Security" +
        advice
    );
    expect(removedToast("laptop", { missed: ["rig", "mini"] }, false)).toMatch(
      /^Removed laptop — rig, mini were offline — they get the new key when they're back — /
    );
    expect(removedToast("laptop", {}, false)).toBe("Removed laptop — access tokens it already has still work" + advice);
  });

  it("every join action carries the settings note", () => {
    expect(joinSettingsNote("mac-mini")).toBe(
      "This computer takes mac-mini's shared settings where mac-mini has them; your own stay where it has none."
    );
    expect(joinSettingsNote("")).toMatch(/^This computer takes the other computer's shared settings/);
  });

  it("Add to my devices says the OTHER way round: the added computer takes this one's settings", () => {
    // add_paired makes the target adopt this device's bundle and start its
    // settings from here — the reverse of joining.
    expect(addPairedNote("laptop")).toBe(
      "laptop takes this computer's shared settings where this one has them; its own stay where this one has none."
    );
    expect(addPairedNote("laptop")).not.toMatch(/^This computer takes/);
  });
});

describe("who runs PR review and issue handling", () => {
  const m = (key: string, automation?: boolean): FleetMember => ({
    key,
    host: key,
    added_at: 0,
    self: false,
    reachable: true,
    version: "",
    same_fleet: true,
    error: "",
    automation,
  });

  it("says nothing when exactly one device runs it", () => {
    expect(automationHint([m("laptop", true), m("mini", false), m("rig", false)])).toBe("");
  });

  it("warns when none or several do", () => {
    expect(automationHint([m("laptop", false), m("mini", false)])).toMatch(/^None of your devices/);
    expect(automationHint([m("laptop", true), m("mini", true)])).toMatch(/^laptop and mini all run/);
  });

  it("ignores devices too old to say, and a group of one", () => {
    expect(automationHint([m("laptop", true), m("mini"), m("rig")])).toBe("");
    expect(automationHint([m("laptop", false)])).toBe("");
    // The server reports a member too old to say as null, not false.
    const old = { ...m("mini"), automation: null };
    expect(automationHint([m("laptop", false), old, { ...old, key: "rig" }])).toBe("");
  });

  it("names the one device that runs them, and offers Run here only on the others", () => {
    const self = (key: string, automation?: boolean) => ({ ...m(key, automation), self: true });
    const elsewhere = automationLine([self("laptop", false), { ...m("ml-rig", true), host: "rig" }, m("mini", false)]);
    expect(elsewhere?.runner?.key).toBe("ml-rig");
    expect(elsewhere?.here).toBe(false);
    expect(elsewhere?.canMove).toBe(true);
    expect(elsewhere?.text).toMatch(/^rig runs PR review and issue handling for all your devices/);
    // On the device that runs them there is no off switch: they're moved
    // from the device that should take them.
    const here = automationLine([self("laptop", true), m("mini", false)]);
    expect(here?.here).toBe(true);
    expect(here?.canMove).toBe(false);
    expect(here?.text).toMatch(/^This device runs PR review and issue handling/);
    expect(here?.text).toMatch(/Run here in Settings → Devices on the device that should run them/);
  });

  it("not exactly one (older members deciding for themselves): the warning, with Run here", () => {
    const both = automationLine([{ ...m("laptop", true), self: true }, m("mini", true)]);
    expect(both?.runner).toBeNull();
    expect(both?.canMove).toBe(true);
    expect(both?.text).toMatch(/^laptop and mini all run/);
    // A group of one, or members too old to say: nothing to show.
    expect(automationLine([{ ...m("laptop", true), self: true }])).toBeNull();
    expect(automationLine([m("laptop"), m("mini")])).toBeNull();
  });
});

describe("removals, key conflicts and the token rotation", () => {
  const member = (key: string, over: Partial<FleetMember> = {}): FleetMember => ({
    key,
    host: key,
    added_at: 0,
    self: key === "laptop",
    reachable: true,
    version: "",
    same_fleet: true,
    error: "",
    ...over,
  });
  const at = (h: number, m: number, day = 8) => new Date(2026, 9, day, h, m).getTime() / 1000;
  const now = at(12, 0);

  it("a removal another device made names who did it — the tell-tale of a forged one", () => {
    const st = {
      self: { key: "laptop", host: "laptop" },
      members: [member("laptop"), member("ml-rig", { host: "rig" })],
      removed: [
        { key: "mini", host: "mac-mini", removed_at: at(10, 32), removed_by: "ml-rig" },
        // This device's own removal: it knows, nothing to flag.
        { key: "old", host: "old-box", removed_at: at(11, 0), removed_by: "laptop" },
      ],
    };
    expect(removalLines(st, now)).toEqual([
      { key: "mini", text: "rig removed mac-mini at 10:32 — if that wasn't you, remove rig from your tailnet" },
    ]);
  });

  it("this device removed by another, an older day, the `at` spelling, and old tombstones", () => {
    const st = {
      self: { key: "laptop", host: "laptop" },
      members: [member("laptop")],
      removed: [
        { key: "laptop", at: at(9, 5, 3), removed_by: "ml-rig", removed_by_host: "rig" },
        // No remover recorded (an older tombstone): nothing to say.
        { key: "x", at: at(9, 0) },
        // Weeks old: off the screen.
        { key: "y", at: now - 30 * 86400, removed_by: "ml-rig" },
      ],
    };
    expect(removalLines(st, now)).toEqual([
      { key: "laptop", text: "rig removed this device on Oct 3, 9:05 — if that wasn't you, remove rig from your tailnet" },
    ]);
    expect(removalLines(null, now)).toEqual([]);
  });

  it("a device that LEFT on its own is not flagged as a possibly forged removal", () => {
    const st = {
      self: { key: "ml-rig", host: "rig" },
      members: [member("ml-rig", { host: "rig", self: true })],
      removed: [
        // The server's `left` flag, and an older server's "removed by itself".
        { key: "laptop", host: "Laptop", removed_at: at(10, 32), removed_by: "laptop", left: true },
        { key: "nas", host: "nas", removed_at: at(11, 5), removed_by: "nas" },
        { key: "mini", host: "mac-mini", removed_at: at(11, 30), removed_by: "laptop", removed_by_host: "Laptop" },
      ],
    };
    expect(removalLines(st, now)).toEqual([
      { key: "mini", text: "Laptop removed mac-mini at 11:30 — if that wasn't you, remove Laptop from your tailnet" },
    ]);
    expect(leftLines(st, now)).toEqual([
      { key: "nas", text: "nas left your devices at 11:05" },
      { key: "laptop", text: "Laptop left your devices at 10:32" },
    ]);
    // Shown as long as a removal is, then gone.
    expect(leftLines(st, at(11, 0) + 15 * 86400)).toEqual([]);
    expect(leftLines(null, now)).toEqual([]);
  });

  it("a device added back on another member is offered back here, naming who added it", () => {
    const st = {
      self: { key: "laptop", host: "laptop" },
      members: [member("laptop"), member("ml-rig", { host: "rig" })],
      readmitted_elsewhere: [{ key: "mini", host: "mac-mini", by: "ml-rig" }, { key: "nas" }],
    };
    expect(readmittedLines(st)).toEqual([
      {
        key: "mini",
        host: "mac-mini",
        text:
          "mac-mini was added back on rig, but it's still removed on this device — it gets no settings sync, sign-in or ticket claims from here until you allow it.",
      },
      {
        key: "nas",
        host: "nas",
        text:
          "nas was added back on another of your devices, but it's still removed on this device — it gets no settings sync, sign-in or ticket claims from here until you allow it.",
      },
    ]);
    expect(readmittedLines({ self: { key: "laptop", host: "laptop" }, members: [] })).toEqual([]);
    // The adder isn't a member row here (it was removed too, or not known
    // yet): the server's by_host names it, not its raw key.
    expect(
      readmittedLines({
        self: { key: "laptop", host: "laptop" },
        members: [],
        readmitted_elsewhere: [{ key: "mini", host: "mac-mini", by: "n123abc", by_host: "ml-rig" }],
      })[0].text
    ).toMatch(/^mac-mini was added back on ml-rig,/);
  });

  it("a member holding a different key is a conflict, flagged or said in its error", () => {
    const ms = [
      member("laptop"),
      member("mini", { key_conflict: true }),
      member("rig", { error: "has a different key for your devices — rejoin one from the other" }),
      member("nas", { error: "offline" }),
    ];
    expect(keyConflicts(ms).map((m) => m.key)).toEqual(["mini", "rig"]);
  });

  it("the rotate toast says the phone must scan again and where the new device key went", () => {
    expect(rotatedToast({})).toBe(
      "Access token regenerated — scan the QR again on your phone; other browsers sign in again"
    );
    expect(rotatedToast({ rekeyed: ["mini"], missed: ["rig"] })).toBe(
      "Access token regenerated — scan the QR again on your phone; other browsers sign in again · new device key sent to mini · rig gets the new device key when back online"
    );
    expect(rotatedToast({ fleet: { rekeyed: ["mini"], missed: [] } })).toMatch(/new device key sent to mini$/);
    // Each member's own access token too: where it was replaced, and where
    // it's still to do by hand (refused, or offline).
    expect(rotatedToast({ rekeyed: ["mini", "nas"], missed: ["rig"], rotated: ["mini"], rotate_failed: ["nas"] })).toBe(
      "Access token regenerated — scan the QR again on your phone; other browsers sign in again · new device key sent to mini, nas · rig gets the new device key when back online · access token replaced on mini · regenerate the access token on nas, rig too (Security there) — theirs still work"
    );
    expect(rotatedToast({ rekeyed: ["mini"], missed: [], rotated: ["mini"], rotate_failed: [] })).toMatch(
      /access token replaced on mini$/
    );
    // The token changed but the devices' key didn't: say so.
    expect(rotatedToast({ fleet_error: "couldn't replace your devices' key: boom" })).toMatch(
      / · couldn't replace your devices' key: boom$/
    );
  });
});
