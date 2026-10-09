/** Joining makes you reachable, says why when it can't, and is approvable
 * from wherever you are: the wording helpers in lib/fleet.ts, the one
 * "Paste a code" router, the sidebar's "Add to my devices", the bell's
 * Approve, Make reachable's single save, and the desktop notifications. */

import { afterEach, describe, expect, it, vi } from "vitest";
import type { FleetAdmit, FleetJoin, FleetMember, FleetStatus } from "../api/types";
import {
  MAKE_REACHABLE_TEXT,
  admitLine,
  approvableRequest,
  deviceEventNote,
  joinedToast,
  matchText,
  memberStatus,
  phoneLinkLine,
  requestNote,
  routeCode,
  sidebarDeviceNote,
  thisDeviceLine,
} from "../lib/fleet";
import { desktopNoteFor, desktopNotify } from "../lib/desktopNotify";
import { reachableFields } from "../components/settings/useMakeReachable";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("one Paste a code box routes by format", () => {
  it.each([
    ["abcd-efgh", { kind: "device", code: "ABCD-EFGH", device: "" }],
    ["ABCD EFGH", { kind: "device", code: "ABCD-EFGH", device: "" }],
    ["mindflock devices join rig ABCD-EFGH", { kind: "device", code: "ABCD-EFGH", device: "rig" }],
    ["rig abcdefgh", { kind: "device", code: "ABCD-EFGH", device: "rig" }],
    ["mfp1:abcdefgh234567-wxyz", { kind: "peer", code: "mfp1:abcdefgh234567-wxyz", device: "" }],
    ["Join me: MFP2:ABCDEFGH-WXYZ (10 min)", { kind: "peer", code: "mfp2:abcdefgh-wxyz", device: "" }],
    ["hello there", { kind: "", code: "", device: "" }],
    ["ABCD", { kind: "", code: "", device: "" }],
  ])("%s", (text, want) => {
    expect(routeCode(text)).toEqual(want);
  });
});

const member = (over: Partial<FleetMember>): FleetMember => ({
  key: "rig",
  host: "rig",
  added_at: 1,
  self: false,
  reachable: false,
  version: "",
  same_fleet: false,
  error: "",
  ...over,
});

describe("why a member is offline", () => {
  it("says what discovery found instead of 'offline'", () => {
    expect(memberStatus(member({ reason: "timed out on :8765 — your Tailscale policy may block tcp:8765" }), "")).toMatch(
      /^timed out on :8765/
    );
  });
  it("falls back to when MindFlock last answered there", () => {
    const now = Date.now() / 1000;
    expect(memberStatus(member({ last_seen: now - 7200 }), "")).toBe("offline · last seen 2h ago");
    expect(memberStatus(member({}), "")).toBe("offline");
  });
});

const status = (over: Partial<FleetStatus>): FleetStatus =>
  ({
    in_fleet: true,
    id: "x",
    epoch: 1,
    self: { key: "laptop", host: "Laptop", ip: "100.64.0.1", port: 8765 },
    members: [],
    invites: [],
    requests: [],
    join: { state: "idle", device: "", host: "", code: "", error: "", id: "" },
    stale_key: false,
    gate_warning: false,
    candidates: [],
    ...over,
  }) as FleetStatus;

describe("This device", () => {
  it("a local-only device says nothing else can reach it", () => {
    const line = thisDeviceLine(status({ self_reachable: false, listening: "local", gate_on: false }));
    expect(line).toMatch(/only listens on this computer \(127\.0\.0\.1\)/);
  });
  it("a reachable one says where and whether the gate is on", () => {
    expect(thisDeviceLine(status({ self_reachable: true, listening: "tailnet", gate_on: true }))).toBe(
      "Laptop listens on your tailnet (100.64.0.1:8765) · access gate on"
    );
  });
  it("an older server that doesn't say shows nothing", () => {
    expect(thisDeviceLine(status({}))).toBe("");
  });
  it("Make reachable's confirm names both halves", () => {
    expect(MAKE_REACHABLE_TEXT).toMatch(/Tailscale mode/);
    expect(MAKE_REACHABLE_TEXT).toMatch(/access gate on/);
  });
});

describe("Make reachable is ONE save: never a tailnet bind with the gate off", () => {
  it("turns Tailscale mode and the gate on together", () => {
    expect(reachableFields({ reach: true })).toEqual({ serve_mode: "tailscale", auth_mode: "on" });
  });
  it("Match adds the phone link's name", () => {
    expect(reachableFields({ reach: true, sharedLink: "mindflock" })).toEqual({
      serve_mode: "tailscale",
      auth_mode: "on",
      shared_link: "mindflock",
    });
    // Already reachable: only the link — the bind isn't touched.
    expect(reachableFields({ reach: false, sharedLink: "mindflock" })).toEqual({ shared_link: "mindflock" });
    expect(reachableFields({ reach: false })).toEqual({});
  });
});

describe("after a join", () => {
  const joined = (over: Partial<FleetJoin>): FleetJoin => ({
    state: "joined",
    device: "mini",
    host: "mac-mini",
    code: "",
    error: "",
    id: "",
    ...over,
  });
  it("a local-only joiner is told, and offered the fix", () => {
    const t = joinedToast(joined({ self_reachable: false }));
    expect(t.offerReach).toBe(true);
    expect(t.text).toMatch(/can't reach this one yet/);
  });
  it("a reachable one hears the usual line", () => {
    expect(joinedToast(joined({ self_reachable: true }))).toEqual({
      text: "Joined mac-mini — your settings now follow your other devices",
      offerReach: false,
    });
  });
  it("the admitter hears when it can't reach the device it let in", () => {
    const a: FleetAdmit = {
      device: "rig",
      host: "rig",
      at: 1,
      state: "unreachable_joiner",
      reason: "connection refused on :8765",
    };
    expect(admitLine(a)).toBe(
      "Joined, but rig isn't reachable from here — connection refused on :8765. On rig: Settings → Devices → Make reachable."
    );
    expect(admitLine({ ...a, state: "reachable" })).toBe("");
  });
});

describe("Match my other devices and the phone-link table", () => {
  it("says what would change", () => {
    expect(matchText({ reachable: true, shared_link: "mindflock" })).toBe(
      "Match your other devices: reachable on Tailscale + phone link “mindflock”"
    );
    expect(matchText({ reachable: false, shared_link: "mindflock" })).toBe(
      "Match your other devices: phone link “mindflock”"
    );
    expect(matchText(null)).toBe("");
  });
  it("lists who hosts the phone link and offers Host here", () => {
    const line = phoneLinkLine({
      name: "mindflock",
      hosts: [
        { key: "laptop", host: "laptop", self: true, state: "off" },
        { key: "mac-mini", host: "mac-mini", self: false, state: "hosting" },
        { key: "rig", host: "rig", self: false, state: "hosting" },
        { key: "nas", host: "nas", self: false, state: "waiting" },
      ],
    });
    expect(line?.text).toBe(
      "Phone link “mindflock”: hosted by mac-mini ✓, rig ✓ · nas ⚠ awaiting approval · laptop not hosting"
    );
    expect(line?.hostHere).toBe(true);
    expect(phoneLinkLine(null)).toBeNull();
  });
});

describe("approve from wherever you are", () => {
  it("a request waiting on another member says so", () => {
    expect(
      requestNote({ id: "a", device: "mini", host: "Mini", code: "1", created_at: 0, expires_at: 0, via: "rig", via_host: "Rig" })
    ).toBe("Check the same code shows on Mini. It asked Rig; approving here answers there.");
    expect(requestNote({ id: "a", device: "mini", host: "Mini", code: "1", created_at: 0, expires_at: 0 })).toBe(
      "Check the same code shows on Mini."
    );
  });
  it("the bell's Approve carries the request, its member and its code", () => {
    expect(
      approvableRequest("device.join_requested", {
        id: "0123456789abcdef",
        via: "rig",
        code: "123 456",
        host: "Mini",
      })
    ).toEqual({ id: "0123456789abcdef", via: "rig", code: "123 456", host: "Mini" });
    expect(approvableRequest("device.join_requested", { id: "nope" })).toBeNull();
    expect(approvableRequest("device.joined", { id: "0123456789abcdef" })).toBeNull();
  });
  it("a copied request's toast says where it asked", () => {
    expect(
      deviceEventNote("device.join_requested", { host: "Mini", code: "123 456", via: "rig", via_host: "Rig" })?.toast
    ).toBe("Mini wants to join your devices (asked Rig) — code 123 456");
  });
});

describe("sidebar: Add to my devices, not a token paste", () => {
  const dev = { reachable: true, remote_control: false, needs_token: false, member: false, connected: false };
  it("offers a MindFlock that can join as one of your devices", () => {
    expect(sidebarDeviceNote({ ...dev, fleet_proto: 1 }, 0)).toEqual({
      note: "Not one of your devices yet — joining turns remote control on",
      action: "add",
    });
    expect(sidebarDeviceNote({ ...dev, remote_control: true, needs_token: true, fleet_proto: 1 }, 0)).toEqual({
      note: "Not one of your devices yet",
      action: "add",
    });
  });
  it("keeps the token paste only for one too old to join", () => {
    expect(sidebarDeviceNote({ ...dev, remote_control: true, needs_token: true, fleet_proto: 0 }, 0)).toEqual({
      note: "needs that device's access token",
      action: "connect",
    });
  });
  it("a member and an unreachable device get no join offer", () => {
    expect(sidebarDeviceNote({ ...dev, remote_control: true, member: true, connected: true, fleet_proto: 1 }, 0)).toEqual({
      note: "no sessions",
      action: null,
    });
    expect(sidebarDeviceNote({ ...dev, reachable: false, fleet_proto: 1 }, 0).action).toBeNull();
  });
});

describe("desktop notifications", () => {
  it("say what needs you and which screen the click opens", () => {
    expect(desktopNoteFor("device.join_requested", { host: "Mini", code: "123 456" })).toEqual({
      title: "Mini wants to join your devices",
      body: "Code 123 456 — check it shows the same, then Approve",
      target: "devices",
    });
    expect(desktopNoteFor("peer.link_added", { peer_name: "Ana", sas: "12-34" })?.target).toBe("peer");
    expect(desktopNoteFor("update.available", { version: "0.7.5" })?.title).toBe("MindFlock update 0.7.5 available");
    expect(desktopNoteFor("device.joined", {})).toBeNull();
  });

  it("only in the desktop app, and only while its window isn't focused", () => {
    const show = vi.fn();
    expect(desktopNotify("device.join_requested", { host: "Mini" })).toBe(false); // no window at all
    vi.stubGlobal("window", {});
    vi.stubGlobal("document", { hasFocus: () => false });
    expect(desktopNotify("device.join_requested", { host: "Mini" })).toBe(false); // a browser: no bridge
    vi.stubGlobal("window", { mfnotify: { show, onClick: () => () => {} } });
    expect(desktopNotify("device.join_requested", { host: "Mini" })).toBe(true);
    expect(show).toHaveBeenCalledWith(expect.objectContaining({ target: "devices" }));
    vi.stubGlobal("document", { hasFocus: () => true });
    expect(desktopNotify("device.join_requested", { host: "Mini" })).toBe(false);
    expect(show).toHaveBeenCalledTimes(1);
  });
});

describe("a candidate that stopped answering", () => {
  it("says why instead of 'offline'", async () => {
    const { candidateBlocker } = await import("../lib/fleet");
    const c = {
      device: "box",
      host: "Box",
      version: "",
      fleet_proto: 1,
      reachable: false,
      member: false,
      in_fleet: false,
      same_fleet: false,
      has_token: false,
    };
    expect(candidateBlocker({ ...c, reason: "connection refused on :8765" })).toBe("connection refused on :8765");
    expect(candidateBlocker(c)).toBe("offline");
  });
});
